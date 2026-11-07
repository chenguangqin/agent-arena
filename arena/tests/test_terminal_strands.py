"""Offline tests for the terminal agent: fake Harbor environment + scripted Strands model."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

from arena_agents.terminal_strands import StrandsTerminalAgent, truncate
from tests.fakes import ScriptedModel, text, tool


class FakeEnv:
    """Records commands; `fail` / `slow` / `big` trigger error, Harbor-timeout and long-output paths."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, str | None, int | None]] = []

    async def exec(self, command: str, cwd=None, env=None, timeout_sec=None, user=None) -> ExecResult:
        argv = shlex.split(command)
        assert argv[:4] == ["timeout", "-k", "5", argv[3]] and argv[4:6] == ["bash", "-c"]
        inner = argv[6]
        self.commands.append((inner, cwd, int(argv[3])))
        if inner == "slow":
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds")
        if inner == "fail":
            return ExecResult(stdout="", stderr="boom", return_code=2)
        if inner == "big":
            return ExecResult(stdout="x" * 50_000, stderr="", return_code=0)
        return ExecResult(stdout=f"ran: {inner}", stderr="", return_code=0)


def make_agent(tmp_path: Path, model: ScriptedModel, **kwargs) -> StrandsTerminalAgent:
    agent = StrandsTerminalAgent(logs_dir=tmp_path, model_name="scripted", **kwargs)
    agent.build_model = lambda: model  # type: ignore[method-assign]
    return agent


def tool_results(model: ScriptedModel) -> list[str]:
    """Texts of tool results the model saw, in order."""
    out = []
    for msg in model.requests[-1]["messages"]:
        for block in msg["content"]:
            if "toolResult" in block:
                out.append(block["toolResult"]["content"][0]["text"])
    return out


def test_truncate_keeps_head_and_tail() -> None:
    s = "A" * 100 + "B" * 100
    t = truncate(s, 60)
    assert t.startswith("A" * 40) and t.endswith("B" * 20) and "140 characters truncated" in t
    assert truncate("short", 60) == "short"


async def test_runs_commands_until_model_stops(tmp_path: Path) -> None:
    model = ScriptedModel([
        [tool("bash", "t1", command="strings /app/bin/x | head")],
        [tool("bash", "t2", command="fail"), tool("bash", "t3", command="slow", timeout_sec=99999)],
        [tool("bash", "t4", command="big")],
        [tool("bash", "t5", command="printf 'YES\\n0x41a54c\\n' > /app/backdoor-detected.txt")],
        [text("Found it.")],
    ])
    agent = make_agent(tmp_path, model, max_output_chars=1000, max_timeout_sec=120)
    env = FakeEnv()
    await agent.setup(env)
    await agent.run("Find the backdoor.", env, AgentContext())
    ctx = AgentContext()
    agent.populate_context_post_run(ctx)

    assert [c[0] for c in env.commands][:3] == ["strings /app/bin/x | head", "fail", "slow"]
    assert all(c[1] == "/app" for c in env.commands)
    assert env.commands[2][2] == 120  # requested timeout clamped to max_timeout_sec
    results = tool_results(model)
    assert "exit_code: 2" in results[1] and "boom" in results[1]
    assert "exit_code: 124" in results[2] and "timed out" in results[2]
    assert len(results[3]) < 1200 and "characters truncated" in results[3]
    assert model.requests[0]["tools"] == ["bash"]
    assert model.requests[0]["messages"][0]["content"][0]["text"] == "Find the backdoor."
    assert ctx.metadata["tool_calls"] == 5 and ctx.metadata["tool_errors"] == 2
    assert ctx.metadata["tool_timeouts"] == 1 and ctx.metadata["termination"] == "end_turn"
    assert ctx.n_input_tokens and ctx.n_output_tokens
    lines = (tmp_path / "commands.jsonl").read_text().splitlines()
    assert len(lines) == 5 and json.loads(lines[0])["exit_code"] == 0
    assert (tmp_path / "strands_messages.json").exists()
    assert (tmp_path / "final_message.txt").read_text().strip() == "Found it."


async def test_tool_budget_stops_loop(tmp_path: Path) -> None:
    model = ScriptedModel([
        [tool("bash", "t1", command="ls")],
        [tool("bash", "t2", command="ls -la")],
        [text("never reached")],
    ])
    agent = make_agent(tmp_path, model, max_tool_calls=2)
    env = FakeEnv()
    await agent.setup(env)
    await agent.run("task", env, AgentContext())
    assert len(env.commands) == 2
    assert agent._stats["termination"] == "max_tool_calls"
    assert model.turns == [[text("never reached")]]  # no model call after the budget ran out
