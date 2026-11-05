"""Command Adapter v0.2 entry point (docs/AGENT_PROTOCOL.md).

Reads one JSON request from stdin, connects to the MCP gateway announced in the
environment, runs the lab's driver, and writes the result file. Diagnostic
artifacts go to ``<agent_root>/reports/``.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.budget import Budget
from core.config import Config, load_profile
from core.lab import Lab, RunContext, default_driver
from core.trace import Trace

SCHEMA_VERSION = "0.2"


def final_text(result: Any) -> str:
    message = getattr(result, "message", None)
    if not isinstance(message, dict):
        return str(result).strip()
    parts = [b["text"] for b in message.get("content", []) if isinstance(b.get("text"), str)]
    return "\n".join(parts).strip()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def mcp_client_from_env() -> Any:
    from strands.tools.mcp import MCPClient

    transport = os.environ.get("AGENT_EVAL_MCP_TRANSPORT", "streamable-http")
    if transport != "streamable-http":
        raise RuntimeError(f"unsupported MCP transport: {transport}")
    headers = json.loads(os.environ.get("AGENT_EVAL_MCP_HEADERS") or "{}")
    return MCPClient(url=os.environ["AGENT_EVAL_MCP_URL"], headers=headers)


def new_context(
    lab: Lab,
    config: Config,
    agent_root: Path,
    instruction: str,
    *,
    trace_path: Path | None,
    model_factory: Callable[[str | None], Any] | None = None,
) -> RunContext:
    budget = Budget(max_total_tokens=config.get("limits", "max_total_tokens"))
    trace = Trace(trace_path, budget, redact=(os.environ.get("AGENT_EVAL_MCP_TOKEN", ""),))
    ctx = RunContext(
        config=config,
        agent_root=agent_root,
        instruction=instruction,
        trace=trace,
        budget=budget,
        model_factory=model_factory,
    )
    ctx.summary.update(lab=lab.name, profile=config.profile, config=config.as_dict())
    return ctx


def finish_summary(ctx: RunContext, result: Any, started: float) -> dict[str, Any]:
    stop_reason = str(getattr(result, "stop_reason", "end_turn"))
    ctx.summary.update(
        stop_reason=stop_reason,
        tool_calls=ctx.trace.tool_calls,
        tool_errors=ctx.trace.tool_errors,
        events=dict(ctx.trace.counts),
        usage=ctx.budget.as_dict(),
        seconds=round(time.monotonic() - started, 2),
    )
    ctx.trace.emit("end", reason=stop_reason)
    return ctx.summary


class Interrupted:
    """Stand-in result when the agent loop ends abnormally but the run itself is valid."""

    def __init__(self, stop_reason: str, detail: str) -> None:
        self.stop_reason = stop_reason
        self.message = {"role": "assistant", "content": [{"text": f"[stopped: {stop_reason}] {detail}"}]}


def drive(lab: Lab, ctx: RunContext) -> Any:
    """Run the lab's driver; an output-limit stop is a legitimate end state, not a crash."""
    from strands.types.exceptions import MaxTokensReachedException

    try:
        return (lab.driver or default_driver)(lab, ctx)
    except MaxTokensReachedException:
        ctx.trace.emit("max_tokens", detail="model output hit max_tokens; loop ended")
        return Interrupted("max_tokens", "model output hit the max_tokens limit")


def run_request(
    lab: Lab,
    request: dict[str, Any],
    config: Config,
    model_factory: Callable[[str | None], Any] | None = None,
) -> dict[str, Any]:
    """Run one task through the protocol; ``model_factory`` is injectable for tests."""
    agent_root = Path(request.get("paths", {}).get("agent_root") or os.getcwd())
    ctx = new_context(
        lab,
        config,
        agent_root,
        request["task"]["instruction"],
        trace_path=agent_root / "reports" / "trace.jsonl",
        model_factory=model_factory,
    )
    ctx.summary["task_id"] = request["task"].get("id")
    started = time.monotonic()
    with mcp_client_from_env() as mcp, ctx.resources:
        ctx.env_tools = mcp.list_tools_sync()
        ctx.env_client = mcp
        ctx.env_url = os.environ["AGENT_EVAL_MCP_URL"]
        ctx.env_headers = json.loads(os.environ.get("AGENT_EVAL_MCP_HEADERS") or "{}")
        result = drive(lab, ctx)
    write_json(ctx.reports_dir / "summary.json", finish_summary(ctx, result, started))
    usage = ctx.budget
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "final_text": final_text(result),
        "structured_output": None,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "source": "sdk",
        },
        "artifacts": ["reports/trace.jsonl", "reports/summary.json"],
    }


def main(lab: Lab, profile: str) -> int:
    result_path = Path(os.environ["AGENT_EVAL_RESULT"])
    try:
        request = json.loads(sys.stdin.readline())
        requested = request.get("agent_config", {}).get("profile", profile)
        if requested != profile:
            raise ValueError(f"profile mismatch: argv={profile!r}, request={requested!r}")
        result = run_request(lab, request, load_profile(profile, lab.profiles_dir, lab.schema))
    except Exception as exc:  # every failure is reported through the protocol
        traceback.print_exc(file=sys.stderr)
        write_json(
            result_path,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "error",
                "final_text": f"{type(exc).__name__}: {exc}",
                "structured_output": None,
                "usage": {"input_tokens": None, "output_tokens": None, "source": "unavailable"},
                "artifacts": [],
            },
        )
        return 1
    write_json(result_path, result)
    return 0
