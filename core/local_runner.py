"""Run a lab agent against a devstore scenario through the v0.2 protocol.

Mimics the evaluation harness: an isolated agent_root holding copies of the
lab's state directories, one JSON request on stdin, MCP connection details in
the environment, and a result file read back after the process exits.

    uv run python -m core.local_runner --lab lab5 --profile baseline
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from core.chat import prepare_agent_root
from core.lab import Lab

REPO = Path(__file__).resolve().parents[1]
ENV_PASSTHROUGH = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}


def load_lab(name: str) -> Lab:
    return importlib.import_module(name).LAB


def child_env(server, run_id: str, root: Path, model_id: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ENV_PASSTHROUGH or k.startswith("AWS_")}
    env.update(
        AGENT_EVAL_RUN_ID=run_id,
        AGENT_EVAL_INPUT=str(root / "run" / "request.json"),
        AGENT_EVAL_RESULT=str(root / "run" / "result.json"),
        AGENT_EVAL_ARTIFACT_DIR=str(root / "reports"),
        AGENT_EVAL_MCP_TRANSPORT="streamable-http",
        AGENT_EVAL_MCP_URL=server.url or "",
        AGENT_EVAL_MCP_TOKEN=server.token,
        AGENT_EVAL_MCP_HEADERS=json.dumps(server.headers),
        # Importable without installing the repository.
        PYTHONPATH=str(REPO),
    )
    if model_id:
        env["MODEL_ID"] = model_id
    return env


def run_once(lab: Lab, profile: str, scenario_id: str, seed: int, model_id: str | None,
             timeout: float, keep: bool) -> dict:
    from devstore import scenarios
    from devstore.server import DevStoreServer

    scenario = scenarios.get(scenario_id)
    run_id = uuid.uuid4().hex[:12]
    base = Path(tempfile.mkdtemp(prefix=f"{lab.name}-{run_id}-"))
    root = prepare_agent_root(lab, base)
    request = {
        "schema_version": "0.2",
        "run_id": run_id,
        "variant": {"id": f"local-{lab.name}-{profile}", "digest": "local"},
        "task": {"id": scenario.ID, "instruction": scenario.INSTRUCTION},
        "agent_config": {"profile": profile},
        "paths": {
            "agent_root": str(root),
            "artifact_dir": str(root / "reports"),
            "result_path": str(root / "run" / "result.json"),
        },
        "constraints": {"deadline_seconds": timeout},
    }
    (root / "run" / "request.json").write_text(json.dumps(request, ensure_ascii=False))

    env = scenario.build(seed)
    with DevStoreServer(env) as server:
        proc = subprocess.run(
            [sys.executable, "-m", lab.name, "run", "--profile", profile],
            input=json.dumps(request, ensure_ascii=False) + "\n",
            text=True,
            cwd=REPO,
            env=child_env(server, run_id, root, model_id),
            timeout=timeout,
        )
        calls = list(server.calls)

    result_path = root / "run" / "result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    # Optional, scenario-defined description of the final state (not a score).
    final_state = None
    if hasattr(scenario, "describe"):
        if "final_text" in inspect.signature(scenario.describe).parameters:
            final_state = scenario.describe(env, final_text=result and result["final_text"])
        else:
            final_state = scenario.describe(env)
    summary_path = root / "reports" / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    report = {
        "lab": lab.name,
        "profile": profile,
        "scenario": scenario.ID,
        "exit_code": proc.returncode,
        "status": result and result["status"],
        "stop_reason": summary.get("stop_reason"),
        "features": summary.get("features"),
        "events": summary.get("events"),
        "usage": summary.get("usage"),
        "final_state": final_state,
        "tool_calls": [c["name"] + ("" if c.get("ok") else " (error)") for c in calls],
        "final_text": result and result["final_text"],
    }
    if keep:
        report["agent_root"] = str(root)
    else:
        saved = REPO / "reports" / f"{lab.name}-{profile}-{run_id}"
        shutil.copytree(root / "reports", saved)
        shutil.rmtree(base)
        report["reports"] = str(saved.relative_to(REPO))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lab", required=True)
    parser.add_argument("--profile", default="baseline")
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--keep", action="store_true", help="keep the agent_root directory")
    args = parser.parse_args(argv)
    lab = load_lab(args.lab)
    report = run_once(lab, args.profile, args.scenario or lab.scenario, args.seed,
                      args.model_id, args.timeout, args.keep)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["exit_code"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
