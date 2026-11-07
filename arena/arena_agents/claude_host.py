"""Evaluate Claude Code on Harbor tasks the same way as Kiro CLI (see ``kiro_host`` / ``host_cli``).

``claude -p`` runs headless on the host in a scratch workspace with an isolated HOME; all built-in tools
are disabled (``--tools ""``, or only ``Skill`` when the config has skills), so the model can act only
through the ``container`` MCP bridge. The same config directory as for kiro is translated:

    steering/*.md (inclusion: always, or no front matter) -> <ws>/CLAUDE.md (one section per file)
    steering/*.md (fileMatch / manual / auto)             -> skipped, listed in metadata ``steering_skipped``
    skills/<name>/SKILL.md                                -> <ws>/.claude/skills/<name>/SKILL.md
    prompt.md                                             -> --append-system-prompt (after the shared BASE_PROMPT)

Auth stays on the host: Bedrock by default (``CLAUDE_CODE_USE_BEDROCK=1`` + the host's AWS credentials
files), or ``--ak provider=anthropic`` with ``ANTHROPIC_API_KEY``.

    harbor run -p <tasks> -a arena_agents.claude_host:ClaudeCodeHostAgent \
      -m global.anthropic.claude-opus-4-6-v1 --ak config_dir=/path/to/configs/steering -e docker
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from harbor.models.agent.context import AgentContext
from pydantic import Field

from arena_agents.host_cli import MCP_NAME, HostCliAgent, HostCliOptions, load_config

DEFAULT_MODEL = "global.anthropic.claude-opus-4-6-v1"
BRIDGE_TOOLS = ("bash", "read_file", "write_file")
MCP_PREFIX = f"mcp__{MCP_NAME}__"

# kiro-style model ids -> Bedrock ids, so one MODEL value can drive both CLIs
MODEL_ALIASES = {
    "claude-opus-4.6": "global.anthropic.claude-opus-4-6-v1",
    "claude-sonnet-4.5": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-haiku-4.5": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}

# Variables an enclosing Claude Code session sets; a child run must not inherit them.
_KEEP_CLAUDE_VARS = {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"}


class ClaudeCodeHostOptions(HostCliOptions):
    claude_bin: str = Field(default="claude", description="claude executable")
    provider: str = Field(default="bedrock", description="bedrock | anthropic")
    region: str = Field(default="", description="AWS region for Bedrock (default: host AWS_REGION or us-west-2)")
    effort: str = Field(default="", description="--effort (low|medium|high|xhigh|max); empty = CLI default")
    max_turns: int = Field(default=0, description="--max-turns (0 = unlimited)")
    max_budget_usd: float = Field(default=0.0, description="--max-budget-usd (0 = unlimited)")


def claude_env(home: Path, provider: str = "bedrock", region: str = "", mcp_timeout_ms: int = 0) -> dict[str, str]:
    real_home = Path(os.environ.get("HOME", "~")).expanduser()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("KIRO_", "CLAUDE_CODE_", "CLAUDECODE")) or k in _KEEP_CLAUDE_VARS}
    for k in ("CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR", "AI_AGENT",
              "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        env.pop(k, None)
    env["HOME"] = str(home)
    if provider == "bedrock":
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        env["AWS_REGION"] = region or os.environ.get("AWS_REGION") or "us-west-2"
        env.pop("ANTHROPIC_API_KEY", None)
        for var, name in (("AWS_CONFIG_FILE", "config"), ("AWS_SHARED_CREDENTIALS_FILE", "credentials")):
            if var not in env and (real_home / ".aws" / name).is_file():  # HOME moved; keep the host's creds
                env[var] = str(real_home / ".aws" / name)
    elif provider == "anthropic":
        env.pop("CLAUDE_CODE_USE_BEDROCK", None)
    else:
        raise ValueError(f"unknown provider {provider!r}")
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    if mcp_timeout_ms:
        env["MCP_TOOL_TIMEOUT"] = str(mcp_timeout_ms)
    env.setdefault("MCP_TIMEOUT", "30000")
    return env


def summarize_stream(path: Path) -> dict[str, Any]:
    """Run status, tool usage, cost and tokens from ``claude -p --output-format stream-json --verbose``."""
    out: dict[str, Any] = {"status": None, "stop_reason": None, "final_text": "", "tool_calls": 0,
                           "tool_failures": 0, "tools": {}, "session_id": None, "model": None,
                           "mcp_status": None, "num_turns": None, "cost_usd": None, "duration_ms": None,
                           "duration_api_ms": None, "usage": None}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            out["session_id"] = ev.get("session_id")
            out["model"] = ev.get("model")
            out["mcp_status"] = {s.get("name"): s.get("status") for s in ev.get("mcp_servers") or []}
        elif kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "?").removeprefix(MCP_PREFIX)
                    out["tool_calls"] += 1
                    out["tools"][name] = out["tools"].get(name, 0) + 1
        elif kind == "user":
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    out["tool_failures"] += 1
        elif kind == "result":
            ok = ev.get("subtype") == "success" and not ev.get("is_error")
            out.update(status="success" if ok else (ev.get("subtype") or "error"),
                       stop_reason=ev.get("stop_reason") or ev.get("subtype"),
                       final_text=ev.get("result") or "", num_turns=ev.get("num_turns"),
                       cost_usd=ev.get("total_cost_usd"), duration_ms=ev.get("duration_ms"),
                       duration_api_ms=ev.get("duration_api_ms"), usage=ev.get("usage"),
                       session_id=ev.get("session_id") or out["session_id"])
    return out


class ClaudeCodeHostAgent(HostCliAgent):
    options_model = ClaudeCodeHostOptions
    cli_label = "claude"
    stream_name = "claude.jsonl"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._has_skills = False

    @staticmethod
    def name() -> str:
        return "claude-code-host"

    @property
    def opts(self) -> ClaudeCodeHostOptions:
        assert isinstance(self.options, ClaudeCodeHostOptions)
        return self.options

    @property
    def model_id(self) -> str:
        m = self.model_name or DEFAULT_MODEL
        return MODEL_ALIASES.get(m, m)

    def check_host(self) -> None:
        if not shutil.which(self.opts.claude_bin):
            raise RuntimeError(f"{self.opts.claude_bin} not found on PATH")
        if self.opts.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("provider=anthropic needs ANTHROPIC_API_KEY on the host")

    def prepare_workspace(self, root: Path, mcp_url: str) -> Path:
        ws = root / "ws"
        (ws / ".claude").mkdir(parents=True)
        cfg = load_config(self.opts.config_dir)
        always = [d for d in cfg.steering if d.inclusion in ("always", "")]
        self.stats["steering_skipped"] = [d.name for d in cfg.steering if d not in always]
        if always:
            (ws / "CLAUDE.md").write_text("\n\n".join(d.body for d in always) + "\n", encoding="utf-8")
        if cfg.skills_dir is not None:
            shutil.copytree(cfg.skills_dir, ws / ".claude" / "skills")
            self._has_skills = any((ws / ".claude" / "skills").glob("*/SKILL.md"))
        (ws / ".claude" / "settings.json").write_text(json.dumps({
            "permissions": {"allow": self.allowed_tools(), "defaultMode": "dontAsk"},
        }, indent=1), encoding="utf-8")
        (root / "mcp.json").write_text(json.dumps({"mcpServers": {MCP_NAME: {"type": "http", "url": mcp_url}}}),
                                       encoding="utf-8")
        (root / "system_prompt.md").write_text(cfg.prompt, encoding="utf-8")
        return ws

    def allowed_tools(self) -> list[str]:
        return [MCP_PREFIX + t for t in BRIDGE_TOOLS] + (["Skill"] if self._has_skills else [])

    def build_argv(self, instruction: str, ws: Path, root: Path) -> list[str]:
        o = self.opts
        argv = [o.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
                "--model", self.model_id,
                "--tools", "Skill" if self._has_skills else "",
                "--mcp-config", str(root / "mcp.json"), "--strict-mcp-config",
                "--setting-sources", "project",
                "--permission-mode", "dontAsk",
                "--allowedTools", ",".join(self.allowed_tools()),
                "--append-system-prompt", (root / "system_prompt.md").read_text(encoding="utf-8")]
        if not self._has_skills:
            argv.append("--disable-slash-commands")
        if o.effort:
            argv += ["--effort", o.effort]
        if o.max_turns:
            argv += ["--max-turns", str(o.max_turns)]
        if o.max_budget_usd:
            argv += ["--max-budget-usd", str(o.max_budget_usd)]
        return argv + ["--", instruction]

    def build_env(self, home: Path) -> dict[str, str]:
        return claude_env(home, self.opts.provider, self.opts.region,
                          mcp_timeout_ms=(self.opts.max_timeout_sec + 120) * 1000)

    def summarize(self, stream_path: Path) -> dict[str, Any]:
        return summarize_stream(stream_path)

    def collect_artifacts(self, home: Path, ws: Path) -> None:
        projects = home / ".claude" / "projects"
        if projects.is_dir():
            shutil.copytree(projects, self.logs_dir / "claude_sessions", dirs_exist_ok=True)

    def populate_context_post_run(self, context: AgentContext) -> None:
        s = self._summary
        usage = s.get("usage") or {}
        context.metadata = {
            **self.common_metadata(),
            "model": s.get("model") or self.model_id,
            "effort": self.opts.effort or "(default)",
            "mcp_status": s.get("mcp_status"), "num_turns": s.get("num_turns"),
            "cost_usd": s.get("cost_usd"), "model_turn_ms": s.get("duration_api_ms"),
            "duration_ms": s.get("duration_ms"), "claude_session_id": s.get("session_id"),
        }
        context.cost_usd = s.get("cost_usd")
        context.n_input_tokens = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0) \
            + (usage.get("cache_creation_input_tokens") or 0) or None
        context.n_cache_tokens = usage.get("cache_read_input_tokens")
        context.n_output_tokens = usage.get("output_tokens")
