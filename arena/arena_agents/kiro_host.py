"""Evaluate Kiro CLI itself on Harbor tasks: kiro-cli runs headless on the host, tools reach the task container.

Shared mechanics (MCP bridge, isolated HOME, process lifecycle, config directory) live in
``arena_agents.host_cli``. Kiro specifics:

- ``<ws>/.kiro/agents/eval.json`` exposes only ``@container`` (built-in shell/fs tools are disabled,
  so kiro cannot touch the host) and ``disclose_context`` (skill/steering loading);
  ``<ws>/.kiro/steering`` and ``<ws>/.kiro/skills`` are copied from the config directory.
- ``kiro-cli chat --no-interactive --agent-engine v3 --output-format stream-json`` with the task
  instruction. Auth is the host's ``KIRO_API_KEY`` (it never enters the container).

    harbor run -p <tasks> -a arena_agents.kiro_host:KiroHostAgent -m claude-opus-4.6 \
      --ak config_dir=/path/to/configs/steering -e docker
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from harbor.models.agent.context import AgentContext
from pydantic import Field

from arena_agents.host_cli import BASE_PROMPT, MCP_NAME, HostCliAgent, HostCliOptions, McpBridge, load_config

__all__ = ["BASE_PROMPT", "DEFAULT_MODEL", "KiroHostAgent", "KiroHostOptions", "_McpBridge", "kiro_env",
           "summarize_stream"]

DEFAULT_MODEL = "claude-opus-4.6"
_McpBridge = McpBridge  # backwards-compatible name


class KiroHostOptions(HostCliOptions):
    engine: str = Field(default="v3", description="kiro-cli --agent-engine")
    kiro_bin: str = Field(default="kiro-cli", description="kiro-cli executable")


def kiro_env(home: Path) -> dict[str, str]:
    """Host env for kiro-cli: isolated HOME, no KIRO_* leftovers from an enclosing kiro session except the API key."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("KIRO_")}
    if os.environ.get("KIRO_API_KEY"):
        env["KIRO_API_KEY"] = os.environ["KIRO_API_KEY"]
    env["HOME"] = str(home)
    for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        env.pop(k, None)
    return env


def summarize_stream(path: Path) -> dict[str, Any]:
    """Pull run status, tool usage and credits out of kiro's stream-json (ACP events)."""
    out: dict[str, Any] = {"status": None, "stop_reason": None, "final_text": "", "tool_calls": 0,
                           "tool_failures": 0, "tools": {}, "credits": 0.0, "turn_ms": 0, "session_id": None}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        data = ev.get("data") or {}
        if ev.get("type") == "metadata":
            if data.get("meteringUsage"):
                out["_metered"] = True
            out["credits"] += sum(float(m.get("value") or 0) for m in data.get("meteringUsage") or [])
            out["turn_ms"] += int(data.get("turnDurationMs") or 0)
        elif ev.get("type") == "sessionUpdate":
            u = data.get("update") or {}
            kind = u.get("sessionUpdate")
            tool_id = ((u.get("_meta") or {}).get("kiro") or {})
            name = tool_id.get("toolName") or tool_id.get("toolId") or u.get("title") or "?"
            if kind == "tool_call":
                out["tool_calls"] += 1
                out["tools"][name] = out["tools"].get(name, 0) + 1
            elif kind == "tool_call_update" and u.get("status") == "failed":
                out["tool_failures"] += 1
        elif ev.get("type") == "runFinished":
            out.update(status=data.get("status"), stop_reason=data.get("stopReason"),
                       final_text=data.get("finalText") or "", session_id=data.get("sessionId"))
    out["credits"] = round(out["credits"], 4) if out.pop("_metered", False) else None  # v3 emits no metering
    return out


class KiroHostAgent(HostCliAgent):
    options_model = KiroHostOptions
    cli_label = "kiro"
    stream_name = "kiro.jsonl"

    @staticmethod
    def name() -> str:
        return "kiro-cli-host"

    @property
    def opts(self) -> KiroHostOptions:
        assert isinstance(self.options, KiroHostOptions)
        return self.options

    def check_host(self) -> None:
        if not shutil.which(self.opts.kiro_bin):
            raise RuntimeError(f"{self.opts.kiro_bin} not found on PATH")
        if not os.environ.get("KIRO_API_KEY"):
            raise RuntimeError("KIRO_API_KEY is not set on the host (needed for kiro-cli headless mode)")

    def prepare_workspace(self, root: Path, mcp_url: str) -> Path:
        ws = root / "ws"
        (ws / ".kiro" / "agents").mkdir(parents=True)
        cfg = load_config(self.opts.config_dir)
        if cfg.root is not None:
            for sub in ("steering", "skills"):  # kiro reads both natively, front matter included
                if (cfg.root / sub).is_dir():
                    shutil.copytree(cfg.root / sub, ws / ".kiro" / sub)
        agent_cfg = {
            "name": "eval",
            "description": "Harbor evaluation agent: only the task-container MCP tools are available",
            "prompt": cfg.prompt,
            "mcpServers": {MCP_NAME: {"url": mcp_url, "timeout": (self.opts.max_timeout_sec + 120) * 1000}},
            "tools": [f"@{MCP_NAME}", "disclose_context"],
            "allowedTools": [f"@{MCP_NAME}", "disclose_context"],
        }
        (ws / ".kiro" / "agents" / "eval.json").write_text(json.dumps(agent_cfg, indent=1), encoding="utf-8")
        return ws

    def build_argv(self, instruction: str, ws: Path, root: Path) -> list[str]:
        return [self.opts.kiro_bin, "chat", "--no-interactive", "--trust-workspace",
                "--agent", "eval", "--agent-engine", self.opts.engine,
                f"--trust-tools=@{MCP_NAME},disclose_context",
                "--model", self.model_name or DEFAULT_MODEL,
                "--output-format", "stream-json", instruction]

    def build_env(self, home: Path) -> dict[str, str]:
        return kiro_env(home)

    def summarize(self, stream_path: Path) -> dict[str, Any]:
        return summarize_stream(stream_path)

    def collect_artifacts(self, home: Path, ws: Path) -> None:
        sessions = home / ".kiro" / "sessions"
        if sessions.is_dir():
            shutil.copytree(sessions, self.logs_dir / "kiro_sessions", dirs_exist_ok=True)

    def populate_context_post_run(self, context: AgentContext) -> None:
        s = self._summary
        context.metadata = {
            **self.common_metadata(),
            "model": self.model_name or DEFAULT_MODEL,
            "engine": self.opts.engine,
            "credits": s.get("credits"), "model_turn_ms": s.get("turn_ms"), "kiro_session_id": s.get("session_id"),
        }
