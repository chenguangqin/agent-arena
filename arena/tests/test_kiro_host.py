"""Offline tests for the Kiro host agent: real MCP bridge over HTTP + fake Harbor environment (no Docker, no kiro)."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from harbor.environments.base import ExecResult
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from arena_agents.kiro_host import KiroHostAgent, _McpBridge, kiro_env, summarize_stream


class FakeEnv:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.files: dict[str, str] = {"/app/notes.txt": "\n".join(f"line{i}" for i in range(1, 11)) + "\n"}

    async def exec(self, command: str, cwd=None, env=None, timeout_sec=None, user=None) -> ExecResult:
        argv = shlex.split(command)
        assert argv[:3] == ["timeout", "-k", "5"] and argv[4:6] == ["bash", "-c"]
        inner = argv[6]
        self.commands.append(inner)
        if inner.startswith("test -f "):
            path = shlex.split(inner)[2]
            if path not in self.files:
                return ExecResult(stdout="", stderr="", return_code=1)
            rng = shlex.split(inner)[6]  # test -f P && sed -n 'a,bp' P | cat -n
            a, b = (int(x) for x in rng.rstrip("p").split(","))
            lines = self.files[path].splitlines()[a - 1:b]
            return ExecResult(stdout="".join(f"{i:6d}\t{ln}\n" for i, ln in enumerate(lines, 1)), stderr="",
                              return_code=0)
        if inner == "fail":
            return ExecResult(stdout="", stderr="boom", return_code=3)
        return ExecResult(stdout=f"ran: {inner}", stderr="", return_code=0)

    async def upload_file(self, source, target) -> None:
        self.files[target] = Path(source).read_text()


def make_agent(tmp_path: Path, **kwargs) -> KiroHostAgent:
    return KiroHostAgent(logs_dir=tmp_path, model_name="claude-opus-4.6", **kwargs)


async def call(url: str, tool: str, args: dict) -> tuple[bool, str]:
    async with streamable_http_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
            return bool(res.is_error), "".join(c.text for c in res.content if hasattr(c, "text"))


async def test_bridge_tools_run_in_container(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    env = FakeEnv()
    bridge = _McpBridge(agent, env)
    await bridge.start()
    try:
        assert bridge.url.startswith("http://127.0.0.1:") and "/mcp-" in bridge.url
        err, out = await call(bridge.url, "bash", {"command": "strings /app/bin/x"})
        assert not err and "exit_code: 0" in out and "ran: strings /app/bin/x" in out
        err, out = await call(bridge.url, "bash", {"command": "fail"})
        assert "exit_code: 3" in out and "boom" in out
        err, out = await call(bridge.url, "read_file", {"path": "/app/notes.txt", "offset": 3, "limit": 2})
        assert not err and "     4\tline4" in out and "     5\tline5" in out and "line6" not in out
        err, out = await call(bridge.url, "read_file", {"path": "/nope"})
        assert err and "cannot read /nope" in out
        err, out = await call(bridge.url, "write_file", {"path": "/app/out/r.txt", "content": "YES\n0x1\n"})
        assert not err and env.files["/app/out/r.txt"] == "YES\n0x1\n"
        assert "mkdir -p /app/out" in env.commands
        # a wrong path (no secret) is rejected
        bad = bridge.url.rsplit("/", 1)[0] + "/mcp"
        try:
            await call(bad, "bash", {"command": "id"})
            raise AssertionError("expected failure on unguessed path")
        except Exception as exc:  # noqa: BLE001
            assert "expected failure" not in str(exc)
    finally:
        await bridge.stop()
    lines = (tmp_path / "mcp_calls.jsonl").read_text().splitlines()
    assert [json.loads(x)["tool"] for x in lines] == ["bash", "bash", "read_file", "read_file", "write_file"]


def test_workspace_layout_and_isolation(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "cfg"
    (cfg / "steering").mkdir(parents=True)
    (cfg / "steering" / "sec.md").write_text("audit carefully")
    (cfg / "skills" / "scan").mkdir(parents=True)
    (cfg / "skills" / "scan" / "SKILL.md").write_text("---\nname: scan\ndescription: d\n---\nbody")
    (cfg / "prompt.md").write_text("Extra rule.")
    agent = make_agent(tmp_path / "logs", config_dir=str(cfg))
    ws = agent.prepare_workspace(tmp_path / "run", "http://127.0.0.1:1/mcp-x")
    assert (ws / ".kiro/steering/sec.md").read_text() == "audit carefully"
    assert (ws / ".kiro/skills/scan/SKILL.md").exists()
    a = json.loads((ws / ".kiro/agents/eval.json").read_text())
    assert a["tools"] == ["@container", "disclose_context"] and a["mcpServers"]["container"]["url"].endswith("mcp-x")
    assert a["prompt"].endswith("Extra rule.")

    monkeypatch.setenv("KIRO_API_KEY", "kak_test")
    monkeypatch.setenv("KIRO_SESSION_ID", "outer")
    monkeypatch.setenv("XDG_DATA_HOME", "/somewhere")
    env = kiro_env(tmp_path / "home")
    assert env["KIRO_API_KEY"] == "kak_test" and env["HOME"] == str(tmp_path / "home")
    assert "KIRO_SESSION_ID" not in env and "XDG_DATA_HOME" not in env


def test_summarize_stream(tmp_path: Path) -> None:
    events = [
        {"type": "runStarted", "data": {"engine": "v3"}},
        {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "tool_call", "title": "x",
                                                      "_meta": {"kiro": {"toolName": "bash"}}}}},
        {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "tool_call_update", "status": "failed"}}},
        {"type": "metadata", "data": {"meteringUsage": [{"value": 0.25}, {"value": 0.5}], "turnDurationMs": 1200}},
        {"type": "runFinished", "data": {"status": "success", "stopReason": "end_turn", "finalText": "done",
                                         "sessionId": "s1"}},
    ]
    p = tmp_path / "k.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n")
    s = summarize_stream(p)
    assert s["status"] == "success" and s["final_text"] == "done" and s["credits"] == 0.75
    assert s["tool_calls"] == 1 and s["tools"] == {"bash": 1} and s["tool_failures"] == 1 and s["turn_ms"] == 1200
    assert "_metered" not in s
    p.write_text(json.dumps(events[-1]) + "\n")
    assert summarize_stream(p)["credits"] is None  # v3: no metering events
