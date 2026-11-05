"""Offline tests: fake Harbor environment + scripted Strands model (no Docker, no Bedrock)."""

from __future__ import annotations

import base64
import json
import shlex
import sys
import tomllib
from pathlib import Path

from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import MCPServerConfig

from arena_agents import patch_tau3_tasks
from arena_agents.tau3_strands import StrandsTau3Agent, extract_policy
from tests.fakes import ScriptedModel, text, tool

SCHEMAS = [{"type": "function", "function": {
    "name": "get_account", "description": "Look up an account",
    "parameters": {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]},
}}]


class FakeRuntime:
    """Mimics the tau3-runtime MCP server behind the exec bridge."""

    def __init__(self, user_replies: list[str]) -> None:
        self.user_replies = list(user_replies)
        self.calls: list[tuple[str, dict]] = []
        self.uploaded: list[str] = []

    def handle(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if tool == "get_assistant_tool_schemas":
            return {"is_error": False, "text": json.dumps(SCHEMAS)}
        if tool == "start_conversation":
            return {"is_error": False, "text": "user: my card was charged twice"}
        if tool == "get_account":
            if args.get("account_id") != "A1":
                return {"is_error": True, "text": "Error: account not found"}
            return {"is_error": False, "text": '{"account_id": "A1", "status": "active"}'}
        if tool == "submit_assistant_message":
            if not self.user_replies:
                return {"is_error": False, "text": json.dumps({"observation": None, "termination_reason": "user_stop"})}
            reply = self.user_replies.pop(0)
            return {"is_error": False, "text": json.dumps({"observation": reply, "termination_reason": None})}
        if tool in ("end_conversation", "record_termination"):
            return {"is_error": False, "text": "Conversation ended."}
        return {"is_error": True, "text": f"unknown tool {tool}"}


class FakeEnv:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime

    async def exec(self, command: str, timeout_sec=None, **_) -> ExecResult:
        if command.startswith("mkdir"):
            return ExecResult(stdout="", stderr="", return_code=0)
        argv = shlex.split(command)
        assert argv[:2] == ["python3", "/opt/arena/mcp_call.py"]
        assert argv[2] == "http://tau3-runtime:8000/mcp"
        args = json.loads(base64.b64decode(argv[4])) if len(argv) > 4 else {}
        return ExecResult(stdout="noise\n" + json.dumps(self.runtime.handle(argv[3], args)), stderr="", return_code=0)

    async def upload_file(self, source, target) -> None:
        assert Path(source).exists()
        self.runtime.uploaded.append(target)


def make_agent(tmp_path: Path, model: ScriptedModel) -> StrandsTau3Agent:
    agent = StrandsTau3Agent(
        logs_dir=tmp_path,
        model_name="scripted",
        mcp_servers=[MCPServerConfig(name="tau3-runtime", transport="streamable-http",
                                     url="http://tau3-runtime:8000/mcp")],
        max_user_turns=5,
    )
    agent.build_model = lambda: model  # type: ignore[method-assign]
    return agent


INSTRUCTION = "<instructions>be nice</instructions>\n\n<policy>\nRefund only duplicates.\n</policy>"


def test_extract_policy() -> None:
    assert extract_policy(INSTRUCTION) == "Refund only duplicates."


async def test_conversation_until_user_stops(tmp_path: Path) -> None:
    runtime = FakeRuntime(user_replies=["user: account A1", "user: thanks"])
    model = ScriptedModel([
        [text("Which account?")],
        [tool("get_account", "t1", account_id="A9")],
        [tool("get_account", "t2", account_id="A1")],
        [text("A1 is active; refund issued.")],
        [text("You're welcome!")],
    ])
    agent = make_agent(tmp_path, model)
    env = FakeEnv(runtime)
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, AgentContext())
    ctx = AgentContext()
    agent.populate_context_post_run(ctx)

    assert runtime.uploaded == ["/opt/arena/mcp_call.py"]
    names = [c[0] for c in runtime.calls]
    assert names[:2] == ["get_assistant_tool_schemas", "start_conversation"]
    assert names.count("submit_assistant_message") == 3
    assert [c[1] for c in runtime.calls if c[0] == "submit_assistant_message"][1] == {
        "message": "A1 is active; refund issued."}
    # harness tools are not exposed to the model
    assert set(model.requests[0]["tools"]) == {"get_account", "end_conversation"}
    assert "Refund only duplicates." in model.requests[0]["system"]
    assert ctx.metadata["termination"] == "user_stop"
    assert ctx.metadata["mcp_errors"] == 1  # A9 not found
    assert ctx.n_output_tokens and ctx.n_input_tokens
    assert (tmp_path / "conversation.jsonl").exists() and (tmp_path / "strands_messages.json").exists()


async def test_end_conversation_tool_stops_loop(tmp_path: Path) -> None:
    runtime = FakeRuntime(user_replies=["user: that's all"])
    model = ScriptedModel([
        [text("Done, anything else?")],
        [tool("end_conversation", "e1")],
    ])
    agent = make_agent(tmp_path, model)
    env = FakeEnv(runtime)
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, AgentContext())
    assert runtime.calls[-1][0] == "end_conversation"
    assert agent._stats["termination"] == "agent_stop"
    assert not model.turns  # no extra model call after ending


def test_patch_task_files_idempotent(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "tau3-banking_knowledge-1"
    (task / "environment").mkdir(parents=True)
    toml = ('[verifier]\ntimeout_sec = 300.0\nenv = { OPENAI_API_KEY = "${OPENAI_API_KEY}", '
            'OPENAI_BASE_URL = "${OPENAI_BASE_URL}" }\n\n[environment]\n'
            'env = { OPENAI_API_KEY = "${OPENAI_API_KEY}" }\n')
    compose = ("services:\n  tau3-runtime:\n    environment:\n      - OPENAI_API_KEY=${OPENAI_API_KEY:-}\n"
               "      - TAU2_USER_REASONING_EFFORT=${TAU2_USER_REASONING_EFFORT:-low}\n    volumes: []\n")
    (task / "task.toml").write_text(toml)
    (task / "environment" / "runtime-server").mkdir()
    (task / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\nRUN git clone --depth=1 \"${TAU2_BENCH_REPO}\" /opt/t\n")
    (task / "environment" / "runtime-server" / "Dockerfile").write_text(
        "FROM python:3.12-slim\nRUN pip install tau2\nCOPY server.py ./\nCMD [\"python3\", \"server.py\"]\n")
    (task / "environment" / "docker-compose.yaml").write_text(compose)
    monkeypatch.setattr(sys, "argv", ["patch", str(tmp_path)])
    for _ in range(2):
        assert patch_tau3_tasks.main() == 0
    t = (task / "task.toml").read_text()
    c = (task / "environment" / "docker-compose.yaml").read_text()
    assert t.count("AWS_BEARER_TOKEN_BEDROCK =") == 1
    assert t.index("AWS_BEARER_TOKEN_BEDROCK") < t.index("[environment]")  # verifier only
    assert "${OPENAI_API_KEY}" not in t
    assert c.count("- AWS_BEARER_TOKEN_BEDROCK=") == 1 and "      - TAU2_USER_LLM_ARGS_JSON=" in c
    assert "AWS_REGION_NAME" in tomllib.loads(t)["verifier"]["env"]
    main_df = (task / "environment" / "Dockerfile").read_text()
    rt_df = (task / "environment" / "runtime-server" / "Dockerfile").read_text()
    assert main_df.count("--branch v1.0.1") == 1
    assert main_df.count("boto3") == 1 and main_df.endswith("RUN pip install --no-cache-dir boto3\n")
    assert rt_df.count("boto3") == 1 and rt_df.index("boto3") < rt_df.index("COPY server.py")


async def test_subclass_extension_points(tmp_path: Path) -> None:
    """Teams subclass the baseline and override build_* hooks."""

    class TeamAgent(StrandsTau3Agent):
        def build_system_prompt(self, policy: str) -> str:
            return "TEAM RULES\n" + super().build_system_prompt(policy)

        def build_tools(self, environment, schemas):
            return [t for t in super().build_tools(environment, schemas) if t.tool_name != "get_account"]

    runtime = FakeRuntime(user_replies=[])
    model = ScriptedModel([[text("Hello, how can I help?")]])
    agent = TeamAgent(
        logs_dir=tmp_path, model_name="scripted",
        mcp_servers=[MCPServerConfig(name="tau3-runtime", transport="streamable-http",
                                     url="http://tau3-runtime:8000/mcp")],
    )
    agent.build_model = lambda: model  # type: ignore[method-assign]
    env = FakeEnv(runtime)
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, AgentContext())
    assert model.requests[0]["system"].startswith("TEAM RULES")
    assert model.requests[0]["tools"] == ["end_conversation"]
