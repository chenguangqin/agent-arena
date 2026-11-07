"""Shared scaffolding for evaluating coding-agent CLIs (kiro-cli, claude, ...) on Harbor tasks.

The CLI under test runs headless on the host; the task lives in Harbor's container. Per trial:

1. ``McpBridge`` starts an in-process streamable-HTTP MCP server (127.0.0.1, random port, random
   URL path) named ``container`` with ``bash`` / ``read_file`` / ``write_file``; every call runs in the
   task's main container via ``environment.exec`` / ``upload_file``.
2. The subclass builds a scratch workspace + isolated HOME, translating one *config directory*
   into the CLI's native format, with the CLI's built-in shell/file tools disabled so it can only act
   through the bridge. Credentials stay on the host.
3. The CLI is spawned (stdout -> ``<stream_name>``, stderr -> ``<cli>.stderr.log``), killed on timeout
   or trial cancellation; then the bridge stops and Harbor's verifier checks the container.

Every CLI therefore sees the same tools, the same base prompt and the same config, so results
are comparable across CLIs. Config directory layout (all optional; empty/omitted = baseline):

    <config_dir>/steering/*.md          -> always-on project guidance (kiro steering / CLAUDE.md)
    <config_dir>/skills/<name>/SKILL.md -> workspace skills (same SKILL.md format for both CLIs)
    <config_dir>/prompt.md              -> appended to the agent/system prompt
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field

from arena_agents.terminal_strands import truncate

MCP_NAME = "container"

BASE_PROMPT = """You are working on a task whose files and tools live in a separate Linux container (the task environment).
Your current directory on this machine is NOT the task, and your built-in file and shell tools are disabled.
Use the `container` MCP tools for everything: `bash` runs a command in the container, `read_file` / `write_file`
read and write container files. All paths refer to the container.
Work autonomously; nobody will answer questions. When the task is done, reply with a short summary."""

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class HostCliOptions(AgentOptions):
    config_dir: str = Field(default="", description="Directory with steering/, skills/, prompt.md (empty = baseline)")
    run_timeout_sec: int = Field(default=7000, description="Kill the CLI after this many seconds")
    default_timeout_sec: int = Field(default=600, description="Default timeout for one bash call")
    max_timeout_sec: int = Field(default=1800, description="Upper bound the model may request for one bash call")
    max_output_chars: int = Field(default=20000, description="Truncate tool output shown to the model")
    workdir: str = Field(default="/app", description="Working directory for bash calls in the container")
    keep_home: bool = Field(default=False, description="Keep the isolated HOME/workspace after the run (debugging)")


@dataclass
class SteeringDoc:
    name: str
    body: str  # without front matter
    inclusion: str = "always"  # kiro: always | fileMatch | manual | auto
    raw: str = ""


@dataclass
class EvalConfig:
    """A CLI-neutral view of one config directory."""

    root: Path | None = None
    steering: list[SteeringDoc] = field(default_factory=list)
    skills_dir: Path | None = None
    prompt_extra: str = ""

    @property
    def prompt(self) -> str:
        return BASE_PROMPT + (f"\n\n{self.prompt_extra}" if self.prompt_extra else "")


def load_config(config_dir: str) -> EvalConfig:
    if not config_dir:
        return EvalConfig()
    root = Path(config_dir).expanduser()
    if not root.is_dir():
        raise RuntimeError(f"config_dir {root} does not exist")
    cfg = EvalConfig(root=root)
    for p in sorted((root / "steering").glob("*.md")) if (root / "steering").is_dir() else []:
        raw = p.read_text(encoding="utf-8")
        m = _FRONT_MATTER.match(raw)
        inclusion = "always"
        if m:
            hit = re.search(r"^inclusion:\s*(\S+)", m.group(1), re.MULTILINE)
            inclusion = hit.group(1).strip("'\"") if hit else "always"
        cfg.steering.append(SteeringDoc(p.stem, raw[m.end():].strip() if m else raw.strip(), inclusion, raw))
    if (root / "skills").is_dir():
        cfg.skills_dir = root / "skills"
    if (root / "prompt.md").is_file():
        cfg.prompt_extra = (root / "prompt.md").read_text(encoding="utf-8").strip()
    return cfg


class McpBridge:
    """Streamable-HTTP MCP server whose tools execute in the Harbor task container."""

    def __init__(self, agent: HostCliAgent, environment: BaseEnvironment) -> None:
        self.agent, self.env = agent, environment
        self.path = f"/mcp-{secrets.token_urlsafe(16)}"
        self.url = ""
        self._server: Any = None
        self._task: asyncio.Task | None = None

    async def _exec(self, command: str, timeout: int) -> tuple[int, str, str]:
        wrapped = f"timeout -k 5 {timeout} bash -c {shlex.quote(command)}"
        try:
            res = await self.env.exec(wrapped, cwd=self.agent.opts.workdir, timeout_sec=timeout + 30)
            return res.return_code, res.stdout or "", res.stderr or ""
        except RuntimeError as exc:  # Harbor's own timeout
            return 124, "", str(exc)

    def _log(self, record: dict[str, Any]) -> None:
        self.agent.stats["mcp_calls"] += 1
        with (self.agent.logs_dir / "mcp_calls.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def build(self) -> Any:
        from mcp.server.mcpserver import MCPServer

        opts = self.agent.opts
        server = MCPServer(name=MCP_NAME)

        @server.tool(description="Run a bash command in the task container (as root, cwd /app by default). "
                                 "Each call is a fresh shell. Returns exit code, stdout and stderr.")
        async def bash(command: str, timeout_sec: int = opts.default_timeout_sec) -> str:
            timeout = max(1, min(int(timeout_sec or opts.default_timeout_sec), opts.max_timeout_sec))
            started = time.monotonic()
            rc, out, err = await self._exec(command, timeout)
            body = f"exit_code: {rc}\n" + (f"(command timed out after {timeout}s)\n" if rc == 124 else "")
            body += (f"stdout:\n{out}\n" if out else "") + (f"stderr:\n{err}\n" if err else "")
            self._log({"tool": "bash", "command": command, "timeout_sec": timeout, "exit_code": rc,
                       "stdout": out[-4000:], "stderr": err[-4000:], "seconds": round(time.monotonic() - started, 2)})
            return truncate(body, opts.max_output_chars)

        @server.tool(description="Read a text file in the task container. Returns lines [offset, offset+limit) "
                                 "with line numbers.")
        async def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
            start, end = max(0, int(offset)) + 1, max(0, int(offset)) + max(1, int(limit))
            cmd = f"test -f {shlex.quote(path)} && sed -n '{start},{end}p' {shlex.quote(path)} | cat -n"
            rc, out, err = await self._exec(cmd, 60)
            self._log({"tool": "read_file", "path": path, "offset": offset, "limit": limit, "exit_code": rc})
            if rc != 0:
                from mcp.server.mcpserver.exceptions import ToolError

                raise ToolError(f"cannot read {path}: {err.strip() or 'not a regular file'}")
            if start > 1:  # cat -n restarts at 1; shift to real line numbers
                out = "\n".join(
                    f"{int(ln[:6]) + start - 1:6d}{ln[6:]}" if ln[:6].strip().isdigit() else ln
                    for ln in out.splitlines())
            return truncate(out, opts.max_output_chars) or "(empty)"

        @server.tool(description="Create or overwrite a file in the task container with the given content.")
        async def write_file(path: str, content: str) -> str:
            parent = str(Path(path).parent)
            await self._exec(f"mkdir -p {shlex.quote(parent)}", 60)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".upload") as tmp:
                tmp.write(content)
            try:
                await self.env.upload_file(tmp.name, path)
            finally:
                os.unlink(tmp.name)
            self._log({"tool": "write_file", "path": path, "bytes": len(content.encode())})
            return f"wrote {len(content.encode())} bytes to {path}"

        return server

    async def start(self) -> None:
        import uvicorn

        app = self.build().streamable_http_app(streamable_http_path=self.path, host="127.0.0.1")
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                break
            if self._task.done():
                self._task.result()  # surface startup errors
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("MCP bridge did not start")
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}{self.path}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=10)


class HostCliAgent(BaseAgent):
    """Template: subclasses implement workspace translation, argv/env, stream parsing and metadata."""

    options_model = HostCliOptions
    cli_label = "cli"  # used for log file names
    stream_name = "cli.jsonl"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stats: dict[str, Any] = {"mcp_calls": 0}
        self._summary: dict[str, Any] = {}

    @property
    def opts(self) -> HostCliOptions:
        assert isinstance(self.options, HostCliOptions)
        return self.options

    def version(self) -> str:
        return "0.2.0"

    # ------------------------------------------------------------ subclass API
    def check_host(self) -> None:
        """Raise if the CLI or its credentials are missing on the host."""

    def prepare_workspace(self, root: Path, mcp_url: str) -> Path:
        raise NotImplementedError

    def build_argv(self, instruction: str, ws: Path, root: Path) -> list[str]:
        raise NotImplementedError

    def build_env(self, home: Path) -> dict[str, str]:
        raise NotImplementedError

    def summarize(self, stream_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def succeeded(self, summary: dict[str, Any]) -> bool:
        return summary.get("status") == "success"

    def collect_artifacts(self, home: Path, ws: Path) -> None:
        """Copy CLI session logs etc. out of the isolated HOME before it is deleted."""

    # ------------------------------------------------------------------ common
    async def setup(self, environment: BaseEnvironment) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.check_host()

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        bridge = McpBridge(self, environment)
        root = Path(tempfile.mkdtemp(prefix=f"{self.cli_label}-host-"))
        home = root / "home"
        home.mkdir()
        ws = root / "ws"
        stream_path = self.logs_dir / self.stream_name
        stderr_path = self.logs_dir / f"{self.cli_label}.stderr.log"
        proc: asyncio.subprocess.Process | None = None
        try:
            await bridge.start()
            ws = self.prepare_workspace(root, bridge.url)
            argv = self.build_argv(instruction, ws, root)
            (self.logs_dir / f"{self.cli_label}_command.json").write_text(json.dumps({
                "argv": [a if a != instruction else "<instruction>" for a in argv],
                "config_dir": self.opts.config_dir,
                "workspace": sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file()),
            }, indent=1), encoding="utf-8")
            with stream_path.open("wb") as out, stderr_path.open("wb") as err:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=ws, env=self.build_env(home), stdout=out, stderr=err,
                    stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout=self.opts.run_timeout_sec)
                except TimeoutError:
                    self.stats["cli_timeout"] = True
                    rc = None
            self.stats["cli_exit_code"] = rc
        finally:
            if proc is not None and proc.returncode is None:  # timeout or Harbor cancelled the trial
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=10)
            await bridge.stop()
            self._summary = self.summarize(stream_path)
            (self.logs_dir / "final_message.txt").write_text(self._summary.get("final_text") or "", encoding="utf-8")
            with contextlib.suppress(Exception):
                self.collect_artifacts(home, ws)
            if not self.opts.keep_home:
                shutil.rmtree(root, ignore_errors=True)
        if not self.succeeded(self._summary):
            tail = stderr_path.read_text(errors="replace")[-1500:] if stderr_path.exists() else ""
            raise RuntimeError(f"{self.cli_label} run did not succeed (exit={self.stats.get('cli_exit_code')}, "
                               f"status={self._summary.get('status')}): {tail}")

    def common_metadata(self) -> dict[str, Any]:
        s = self._summary
        return {
            **self.stats,
            "cli": self.cli_label,
            "config_dir": self.opts.config_dir or "(baseline)",
            "status": s.get("status"), "stop_reason": s.get("stop_reason"),
            "tool_calls": s.get("tool_calls"), "tool_failures": s.get("tool_failures"), "tools": s.get("tools"),
            "session_id": s.get("session_id"),
        }
