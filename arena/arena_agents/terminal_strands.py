"""Baseline Strands agent for terminal-style Harbor tasks (binary-audit, terminal-bench, ...).

Same layout as ``tau3_strands``: the agent loop and Bedrock credentials stay on the host;
the model gets a ``bash`` tool whose commands run in the task's main container via
``environment.exec``. The task instruction is the prompt; the run ends when the model
stops calling tools (or the tool-call budget is used up). The verifier then checks the
container state (for example ``/app/backdoor-detected.txt``).

Baseline on purpose: one tool, a short system prompt, no prompt caching, no retries,
no planning/memory scaffolding. Only safety rails needed for long shell sessions:
per-command timeout (enforced inside the container too), output truncation, and a
tool-call budget.

    harbor run -p <tasks> -a arena_agents.terminal_strands:StrandsTerminalAgent \
      -m global.anthropic.claude-opus-4-6-v1 -e docker
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field
from strands import Agent
from strands.tools.executors import SequentialToolExecutor
from strands.tools.tools import PythonAgentTool

DEFAULT_MODEL = "global.anthropic.claude-opus-4-6-v1"

SYSTEM_PROMPT = """You are an autonomous engineer working in a Linux container through a `bash` tool.

- Complete the task on your own; nobody will answer questions.
- Each `bash` call runs in a fresh shell (state such as `cd` or variables does not persist; use `&&` or scripts).
- Long outputs are truncated: filter with grep/head/tail, or write results to files and inspect them.
- Write any required output files exactly in the format the task asks for.
- When the task is done, reply with a short summary and stop calling tools.
"""

BASH_SPEC = {
    "name": "bash",
    "description": "Run a bash command in the task container (as root). Returns exit code, stdout and stderr.",
    "inputSchema": {"json": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command or script to run with `bash -c`."},
            "timeout_sec": {"type": "integer", "description": "Optional timeout in seconds (default 600)."},
        },
        "required": ["command"],
    }},
}


def truncate(text: str, limit: int) -> str:
    """Keep the head and tail of long output so errors at the end stay visible."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} characters truncated] ...\n{text[-tail:]}"


class TerminalStrandsOptions(AgentOptions):
    region: str = Field(default="us-west-2", description="Bedrock region")
    max_tokens: int = Field(default=16000, description="Max output tokens per model call")
    max_tool_calls: int = Field(default=300, description="Stop the agent after this many bash calls")
    default_timeout_sec: int = Field(default=600, description="Default timeout for one bash call")
    max_timeout_sec: int = Field(default=1800, description="Upper bound the model may request for one bash call")
    max_output_chars: int = Field(default=20000, description="Truncate stdout+stderr shown to the model")
    workdir: str = Field(default="/app", description="Working directory for bash calls")


class StrandsTerminalAgent(BaseAgent):
    options_model = TerminalStrandsOptions

    @staticmethod
    def name() -> str:
        return "strands-terminal-baseline"

    def version(self) -> str:
        return "0.1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._agent: Agent | None = None
        self._commands_log = self.logs_dir / "commands.jsonl"
        self._stats: dict[str, Any] = {"tool_calls": 0, "tool_errors": 0, "tool_timeouts": 0, "termination": None}

    @property
    def opts(self) -> TerminalStrandsOptions:
        assert isinstance(self.options, TerminalStrandsOptions)
        return self.options

    async def setup(self, environment: BaseEnvironment) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ tools
    async def run_bash(self, environment: BaseEnvironment, command: str, timeout_sec: int | None) -> tuple[bool, str]:
        timeout = max(1, min(int(timeout_sec or self.opts.default_timeout_sec), self.opts.max_timeout_sec))
        # `timeout` inside the container kills the process tree; the outer timeout is a backstop.
        wrapped = f"timeout -k 5 {timeout} bash -c {shlex.quote(command)}"
        started = time.monotonic()
        try:
            res = await environment.exec(wrapped, cwd=self.opts.workdir, timeout_sec=timeout + 30)
            rc, out, err = res.return_code, res.stdout or "", res.stderr or ""
        except RuntimeError as exc:  # Harbor raises on its own timeout
            rc, out, err = 124, "", str(exc)
        timed_out = rc == 124
        body = f"exit_code: {rc}\n"
        if timed_out:
            body += f"(command timed out after {timeout}s)\n"
        if out:
            body += f"stdout:\n{out}\n"
        if err:
            body += f"stderr:\n{err}\n"
        is_error = rc != 0
        self._stats["tool_calls"] += 1
        self._stats["tool_errors"] += int(is_error)
        self._stats["tool_timeouts"] += int(timed_out)
        self._append(self._commands_log, {
            "command": command, "timeout_sec": timeout, "exit_code": rc,
            "stdout": out[-4000:], "stderr": err[-4000:], "seconds": round(time.monotonic() - started, 2),
        })
        return is_error, truncate(body, self.opts.max_output_chars)

    def _bash_tool(self, environment: BaseEnvironment) -> PythonAgentTool:
        async def call(tool_use: dict[str, Any], **invocation_state: Any) -> dict[str, Any]:
            args = tool_use.get("input") or {}
            command = str(args.get("command") or "").strip()
            if not command:
                is_error, text = True, "error: `command` is required"
            else:
                is_error, text = await self.run_bash(environment, command, args.get("timeout_sec"))
            if self._stats["tool_calls"] >= self.opts.max_tool_calls:
                self._stats["termination"] = "max_tool_calls"
                invocation_state.setdefault("request_state", {})["stop_event_loop"] = True
                text += "\n[tool-call budget exhausted; the run is stopping]"
            return {
                "toolUseId": tool_use["toolUseId"],
                "status": "error" if is_error else "success",
                "content": [{"text": text}],
            }

        call.__name__ = "bash"
        return PythonAgentTool("bash", BASH_SPEC, call)

    def build_model(self) -> Any:
        from botocore.config import Config
        from strands.models import BedrockModel

        return BedrockModel(
            model_id=self.model_name or DEFAULT_MODEL,
            region_name=self.opts.region,
            max_tokens=self.opts.max_tokens,
            boto_client_config=Config(read_timeout=900, retries={"max_attempts": 6, "mode": "adaptive"}),
        )

    # -------------------------------------------------------------------- run
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        self._agent = Agent(
            model=self.build_model(),
            tools=[self._bash_tool(environment)],
            system_prompt=SYSTEM_PROMPT,
            tool_executor=SequentialToolExecutor(),
            callback_handler=None,
        )
        try:
            result = await self._agent.invoke_async(instruction)
            self._stats["termination"] = self._stats["termination"] or str(result.stop_reason)
            (self.logs_dir / "final_message.txt").write_text(str(result), encoding="utf-8")
        except Exception as exc:
            self._stats["termination"] = f"error: {type(exc).__name__}"
            raise
        finally:
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
