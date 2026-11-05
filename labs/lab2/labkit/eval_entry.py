"""Arena entry point (Command Adapter v0.2): one JSON request on stdin, result file out."""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from labkit import trace
from labkit.cli import BuildAgent
from labkit.env import Env


def main(build_agent: BuildAgent, mode: str) -> int:
    result_path = Path(os.environ["AGENT_EVAL_RESULT"])
    try:
        request = json.loads(sys.stdin.readline())
        root = Path(request.get("paths", {}).get("agent_root") or os.getcwd())
        trace.configure(root / "reports" / "trace.jsonl", echo=False,
                        redact=(os.environ.get("AGENT_EVAL_MCP_TOKEN", ""),))
        env = Env.from_protocol(request["task"]["instruction"])
        agent = build_agent(env, request.get("agent_config", {}).get("mode", mode))
        agent.hooks.add_hook(trace.Tracer())
        try:
            reply = str(agent(env.instruction))
        finally:
            agent.cleanup()
            env.close()
        totals = trace.totals()
        _write(result_path, {"schema_version": "0.2", "status": "completed", "final_text": reply,
                             "structured_output": None,
                             "usage": {"input_tokens": totals["input_tokens"],
                                       "output_tokens": totals["output_tokens"], "source": "sdk"},
                             "artifacts": ["reports/trace.jsonl"]})
        return 0
    except Exception as exc:  # every failure is reported through the protocol
        traceback.print_exc(file=sys.stderr)
        _write(result_path, {"schema_version": "0.2", "status": "error", "final_text": f"{type(exc).__name__}: {exc}",
                             "structured_output": None,
                             "usage": {"input_tokens": None, "output_tokens": None, "source": "unavailable"},
                             "artifacts": []})
        return 1


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
