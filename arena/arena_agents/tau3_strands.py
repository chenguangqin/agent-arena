"""Baseline Strands agent for τ³-bench tasks in Harbor.

The agent runs on the host (Bedrock credentials stay on the host). The task's
``tau3-runtime`` MCP server lives on the task's private Docker network, so every
tool call is forwarded through ``environment.exec`` into the main container,
where ``mcp_call.py`` performs the MCP request.

Conversation loop (harness-driven, like a chat CLI):

1. ``start_conversation`` → the user's first message becomes the prompt.
2. The Strands agent answers, calling domain tools as needed.
3. Its final text is sent to the simulated user (``submit_assistant_message``);
   the user's reply becomes the next prompt.
4. Stop when the runtime reports a termination reason, or the agent calls
   ``end_conversation``, or ``max_user_turns`` is reached.

Baseline on purpose: no prompt caching, no retries, no context management,
no extra guidance beyond the task policy.

    harbor run -p <tau3 dataset> -i <task> \
      -a arena_agents.tau3_strands:StrandsTau3Agent \
      -m global.anthropic.claude-opus-4-6-v1 -e docker
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field
from strands import Agent
from strands.tools.executors import SequentialToolExecutor
from strands.tools.tools import PythonAgentTool

DEFAULT_MODEL = "global.anthropic.claude-opus-4-6-v1"
BRIDGE_PATH = "/opt/arena/mcp_call.py"
MCP_PIP = "mcp==1.25.0"

SYSTEM_PROMPT = """You are a customer service agent. Help the user according to the policy below.

- The user's messages arrive as your input. Your text reply is sent to the user.
- Use the tools to look up or change information. Do not make up information.
- In each reply either call tools or talk to the user; finish tool work before replying.
- When the user's issue is fully resolved (or transferred), call `end_conversation`.

<policy>
{policy}
</policy>
"""


def extract_policy(instruction: str) -> str:
    matches = re.findall(r"<policy>\s*(.*?)\s*</policy>", instruction, flags=re.DOTALL)
    return matches[-1].strip() if matches else instruction.strip()


class Tau3StrandsOptions(AgentOptions):
    region: str = Field(default="us-west-2", description="Bedrock region")
    max_tokens: int = Field(default=4096, description="Max output tokens per model call")
    max_user_turns: int = Field(default=30, description="Max agent→user messages")
    tool_timeout_sec: int = Field(default=240, description="Timeout for one MCP call")


class StrandsTau3Agent(BaseAgent):
    capabilities = AgentCapabilities(mcp_servers=True)
    options_model = Tau3StrandsOptions

    @staticmethod
    def name() -> str:
        return "strands-tau3-baseline"

    def version(self) -> str:
        return "0.1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mcp_url = ""
        self._ended = False
        self._calls_log = self.logs_dir / "mcp_calls.jsonl"
        self._chat_log = self.logs_dir / "conversation.jsonl"
        self._agent: Agent | None = None
        self._stats: dict[str, Any] = {"mcp_calls": 0, "mcp_errors": 0, "user_turns": 0}

    @property
    def opts(self) -> Tau3StrandsOptions:
        assert isinstance(self.options, Tau3StrandsOptions)
        return self.options

    # ------------------------------------------------------------------ setup
    async def setup(self, environment: BaseEnvironment) -> None:
        servers = [s for s in self.mcp_servers if s.transport == "streamable-http" and s.url]
        if len(servers) != 1:
            raise ValueError(f"expected one streamable-http MCP server, got {self.mcp_servers}")
        self._mcp_url = servers[0].url or ""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        res = await environment.exec(
            f"mkdir -p /opt/arena && python3 -m pip install -q --no-cache-dir {MCP_PIP}",
            timeout_sec=600,
        )
        if res.return_code != 0:
            raise RuntimeError(f"installing {MCP_PIP} failed: {res.stderr or res.stdout}")
        await environment.upload_file(Path(__file__).with_name("mcp_call.py"), BRIDGE_PATH)

    # ------------------------------------------------------------- MCP bridge
    async def call_mcp(
        self, environment: BaseEnvironment, tool: str, arguments: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        payload = base64.b64encode(json.dumps(arguments or {}).encode()).decode()
        cmd = f"python3 {BRIDGE_PATH} {shlex.quote(self._mcp_url)} {shlex.quote(tool)} {payload}"
        started = time.monotonic()
        res = await environment.exec(cmd, timeout_sec=self.opts.tool_timeout_sec)
        lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
        try:
            out = json.loads(lines[-1])
            is_error, text = bool(out["is_error"]), str(out["text"])
        except (IndexError, ValueError, KeyError):
            is_error = True
            text = f"bridge failed (rc={res.return_code}): {(res.stderr or res.stdout or '')[-2000:]}"
        self._stats["mcp_calls"] += 1
        self._stats["mcp_errors"] += int(is_error)
        self._append(self._calls_log, {
            "tool": tool, "arguments": arguments or {}, "is_error": is_error,
            "result": text, "seconds": round(time.monotonic() - started, 2),
        })
        return is_error, text

    # ------------------------------------------------------------------ tools
    def _domain_tool(self, environment: BaseEnvironment, schema: dict[str, Any]) -> PythonAgentTool:
        fn = schema.get("function", schema)
        name = fn["name"]
        spec = {
            "name": name,
            "description": fn.get("description") or name,
            "inputSchema": {"json": fn.get("parameters") or {"type": "object", "properties": {}}},
        }

        async def call(tool_use: dict[str, Any], **_: Any) -> dict[str, Any]:
            is_error, text = await self.call_mcp(environment, name, tool_use.get("input") or {})
            return {
                "toolUseId": tool_use["toolUseId"],
                "status": "error" if is_error else "success",
                "content": [{"text": text or "(empty result)"}],
            }

        call.__name__ = name
        return PythonAgentTool(name, spec, call)

    def _end_tool(self, environment: BaseEnvironment) -> PythonAgentTool:
        spec = {
            "name": "end_conversation",
            "description": "End the conversation once the user's issue has been resolved or transferred.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }

        async def call(tool_use: dict[str, Any], **invocation_state: Any) -> dict[str, Any]:
            is_error, text = await self.call_mcp(environment, "end_conversation", {})
            if not is_error:
                self._ended = True
                invocation_state.setdefault("request_state", {})["stop_event_loop"] = True
            return {
                "toolUseId": tool_use["toolUseId"],
                "status": "error" if is_error else "success",
                "content": [{"text": text}],
            }

        call.__name__ = "end_conversation"
        return PythonAgentTool("end_conversation", spec, call)

    # ---------------------------------------------------- extension points
    # Subclass StrandsTau3Agent and override these to add harness techniques.
    def build_system_prompt(self, policy: str) -> str:
        return SYSTEM_PROMPT.format(policy=policy)

    def build_tools(self, environment: BaseEnvironment, schemas: list[dict[str, Any]]) -> list[Any]:
        """Domain tools (forwarded to the task's MCP server) plus end_conversation."""
        return [self._domain_tool(environment, s) for s in schemas] + [self._end_tool(environment)]

    def build_agent(self, tools: list[Any], system_prompt: str) -> Agent:
        return Agent(
            model=self.build_model(),
            tools=tools,
            system_prompt=system_prompt,
            tool_executor=SequentialToolExecutor(),
            callback_handler=None,
        )

    def build_model(self) -> Any:
        from strands.models import BedrockModel

        return BedrockModel(
            model_id=self.model_name or DEFAULT_MODEL,
            region_name=self.opts.region,
            max_tokens=self.opts.max_tokens,
        )

    # -------------------------------------------------------------------- run
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        is_error, schemas_text = await self.call_mcp(environment, "get_assistant_tool_schemas")
        if is_error:
            raise RuntimeError(f"get_assistant_tool_schemas failed: {schemas_text}")
        tools = self.build_tools(environment, json.loads(schemas_text))
        self._agent = self.build_agent(tools, self.build_system_prompt(extract_policy(instruction)))

        is_error, user_message = await self.call_mcp(environment, "start_conversation")
        if is_error:
            raise RuntimeError(f"start_conversation failed: {user_message}")
        self._append(self._chat_log, {"role": "user", "content": user_message})

        termination = None
        try:
            for _ in range(self.opts.max_user_turns):
                result = await self._agent.invoke_async(user_message)
                if self._ended:
                    termination = "agent_stop"
                    break
                reply = str(result).strip() or "Could you tell me more about what you need?"
                self._append(self._chat_log, {"role": "assistant", "content": reply})
                self._stats["user_turns"] += 1
                is_error, raw = await self.call_mcp(environment, "submit_assistant_message", {"message": reply})
                if is_error:
                    raise RuntimeError(f"submit_assistant_message failed: {raw}")
                step = json.loads(raw)
                termination = step.get("termination_reason")
                if termination:
                    break
                user_message = step.get("observation") or ""
                self._append(self._chat_log, {"role": "user", "content": user_message})
            else:
                termination = "max_steps"
                await self.call_mcp(environment, "record_termination", {"reason": "max_steps"})
        finally:
            self._stats["termination"] = termination
            self._save_messages()

    # ---------------------------------------------------------------- metrics
    def populate_context_post_run(self, context: AgentContext) -> None:
        if self._agent is None:
            return
        usage = self._agent.event_loop_metrics.accumulated_usage
        cache_read = int(usage.get("cacheReadInputTokens", 0) or 0)
        cache_write = int(usage.get("cacheWriteInputTokens", 0) or 0)
        context.n_input_tokens = int(usage.get("inputTokens", 0)) + cache_read + cache_write
        context.n_cache_tokens = cache_read
        context.n_output_tokens = int(usage.get("outputTokens", 0))
        cycles = self._agent.event_loop_metrics.cycle_count
        context.metadata = {**self._stats, "model_calls": cycles, "model": self.model_name or DEFAULT_MODEL}

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _append(path: Path, record: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _save_messages(self) -> None:
        if self._agent is not None:
            (self.logs_dir / "strands_messages.json").write_text(
                json.dumps(self._agent.messages, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
            )
