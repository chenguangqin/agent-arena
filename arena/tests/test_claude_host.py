"""Offline tests for the Claude Code host agent: config translation, argv, env isolation, stream parsing."""

from __future__ import annotations

import json
from pathlib import Path

from harbor.models.agent.context import AgentContext

from arena_agents.claude_host import ClaudeCodeHostAgent, claude_env, summarize_stream
from arena_agents.host_cli import BASE_PROMPT


def make_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "cfg"
    (cfg / "steering").mkdir(parents=True)
    (cfg / "steering" / "sec.md").write_text("---\ninclusion: always\n---\n# Sec\naudit carefully\n")
    (cfg / "steering" / "plain.md").write_text("no front matter\n")
    (cfg / "steering" / "later.md").write_text("---\ninclusion: manual\n---\nonly on request\n")
    (cfg / "skills" / "scan").mkdir(parents=True)
    (cfg / "skills" / "scan" / "SKILL.md").write_text("---\nname: scan\ndescription: d\n---\nbody")
    (cfg / "prompt.md").write_text("Extra rule.")
    return cfg


def test_workspace_translation_and_argv(tmp_path: Path) -> None:
    agent = ClaudeCodeHostAgent(logs_dir=tmp_path / "logs", model_name="claude-opus-4.6",
                                config_dir=str(make_cfg(tmp_path)), effort="high")
    root = tmp_path / "run"
    ws = agent.prepare_workspace(root, "http://127.0.0.1:1/mcp-x")
    claude_md = (ws / "CLAUDE.md").read_text()
    assert "audit carefully" in claude_md and "no front matter" in claude_md
    assert "inclusion" not in claude_md and "only on request" not in claude_md
    assert agent.stats["steering_skipped"] == ["later"]
    assert (ws / ".claude/skills/scan/SKILL.md").exists()
    assert json.loads((root / "mcp.json").read_text()) == {
        "mcpServers": {"container": {"type": "http", "url": "http://127.0.0.1:1/mcp-x"}}}

    argv = agent.build_argv("do the task", ws, root)
    assert argv[-2:] == ["--", "do the task"]
    assert argv[argv.index("--model") + 1] == "global.anthropic.claude-opus-4-6-v1"  # kiro alias mapped
    assert argv[argv.index("--tools") + 1] == "Skill"
    assert argv[argv.index("--allowedTools") + 1] == \
        "mcp__container__bash,mcp__container__read_file,mcp__container__write_file,Skill"
    assert argv[argv.index("--append-system-prompt") + 1] == BASE_PROMPT + "\n\nExtra rule."
    assert "--strict-mcp-config" in argv and argv[argv.index("--effort") + 1] == "high"
    assert "--disable-slash-commands" not in argv


def test_baseline_has_no_tools_besides_bridge(tmp_path: Path) -> None:
    agent = ClaudeCodeHostAgent(logs_dir=tmp_path / "logs")
    root = tmp_path / "run"
    ws = agent.prepare_workspace(root, "http://127.0.0.1:1/mcp-x")
    assert not (ws / "CLAUDE.md").exists() and not (ws / ".claude/skills").exists()
    argv = agent.build_argv("t", ws, root)
    assert argv[argv.index("--tools") + 1] == "" and "--disable-slash-commands" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == BASE_PROMPT


def test_env_isolation(tmp_path: Path, monkeypatch) -> None:
    real_home = tmp_path / "real"
    (real_home / ".aws").mkdir(parents=True)
    (real_home / ".aws" / "credentials").write_text("[default]\n")
    monkeypatch.setenv("HOME", str(real_home))
    for k, v in {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "outer", "CLAUDE_CODE_ENTRYPOINT": "cli",
                 "CLAUDE_EFFORT": "medium", "KIRO_API_KEY": "k", "ANTHROPIC_API_KEY": "a",
                 "XDG_CONFIG_HOME": "/x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    env = claude_env(tmp_path / "home", "bedrock", "us-east-1", mcp_timeout_ms=5000)
    assert env["HOME"] == str(tmp_path / "home") and env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_REGION"] == "us-east-1" and env["MCP_TOOL_TIMEOUT"] == "5000"
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == str(real_home / ".aws" / "credentials")
    for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT", "KIRO_API_KEY",
              "ANTHROPIC_API_KEY", "XDG_CONFIG_HOME"):
        assert k not in env
    env = claude_env(tmp_path / "home", "anthropic")
    assert env["ANTHROPIC_API_KEY"] == "a" and "CLAUDE_CODE_USE_BEDROCK" not in env


def test_summarize_stream_and_metadata(tmp_path: Path) -> None:
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "m",
         "mcp_servers": [{"name": "container", "status": "connected"}]},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "t1", "name": "mcp__container__bash", "input": {"command": "ls"}},
            {"type": "tool_use", "id": "t2", "name": "Skill", "input": {"skill": "scan"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True},
                                                 {"type": "tool_result", "tool_use_id": "t2"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 3,
         "total_cost_usd": 0.5, "duration_ms": 1000, "duration_api_ms": 800, "session_id": "s1",
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5,
                   "output_tokens": 7}},
    ]
    p = tmp_path / "c.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n")
    s = summarize_stream(p)
    assert s["status"] == "success" and s["final_text"] == "done" and s["cost_usd"] == 0.5
    assert s["tools"] == {"bash": 1, "Skill": 1} and s["tool_calls"] == 2 and s["tool_failures"] == 1
    assert s["mcp_status"] == {"container": "connected"}

    agent = ClaudeCodeHostAgent(logs_dir=tmp_path / "logs")
    agent._summary = s
    ctx = AgentContext()
    agent.populate_context_post_run(ctx)
    assert ctx.cost_usd == 0.5 and ctx.n_input_tokens == 115 and ctx.n_cache_tokens == 100
    assert ctx.metadata["tool_calls"] == 2 and ctx.metadata["cli"] == "claude"

    p.write_text(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}) + "\n")
    assert summarize_stream(p)["status"] == "error_max_turns"
