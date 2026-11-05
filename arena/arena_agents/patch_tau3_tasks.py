"""Patch generated τ³-bench Harbor tasks so the user simulator / verifier can use Bedrock.

The upstream task template only forwards OPENAI_* variables into the runtime sidecar
and verifier. This adds (idempotently):

- runtime sidecar (docker-compose.yaml): TAU2_USER_LLM_ARGS_JSON, AWS_BEARER_TOKEN_BEDROCK, AWS_REGION_NAME
- verifier env (task.toml): AWS_BEARER_TOKEN_BEDROCK, AWS_REGION_NAME
- `${OPENAI_API_KEY}` / `${OPENAI_BASE_URL}` get empty defaults so they may be unset.
- both images (runtime sidecar and main/verifier) install boto3, which LiteLLM needs for Bedrock.
- both images clone tau2-bench at a pinned ref (TAU2_REF, default v1.0.1) instead of main:
  the Harbor 0.24 verifier calls the 1.x evaluator API (2.0.0 added a required argument).

AWS credentials are never added; only the short-term Bedrock bearer token.

    python -m arena_agents.patch_tau3_tasks <dataset-dir>
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

COMPOSE_VARS = [
    "TAU2_USER_LLM_ARGS_JSON=${TAU2_USER_LLM_ARGS_JSON:-}",
    "AWS_BEARER_TOKEN_BEDROCK=${AWS_BEARER_TOKEN_BEDROCK:-}",
    "AWS_REGION_NAME=${AWS_REGION_NAME:-us-west-2}",
]
VERIFIER_VARS = (
    'AWS_BEARER_TOKEN_BEDROCK = "${AWS_BEARER_TOKEN_BEDROCK:-}", '
    'AWS_REGION_NAME = "${AWS_REGION_NAME:-us-west-2}", '
)


def patch_compose(text: str) -> str:
    anchor = re.search(r"^(\s*)- TAU2_USER_REASONING_EFFORT=.*$", text, flags=re.MULTILINE)
    if anchor is None:
        raise ValueError("TAU2_USER_REASONING_EFFORT line not found in docker-compose.yaml")
    indent = anchor.group(1)
    missing = [v for v in COMPOSE_VARS if v.split("=", 1)[0] + "=" not in text]
    if not missing:
        return text
    extra = "".join(f"\n{indent}- {v}" for v in missing)
    return text[: anchor.end()] + extra + text[anchor.end():]


TAU2_REF = os.environ.get("TAU2_REF", "v1.0.1")
CLONE = 'git clone --depth=1 "${TAU2_BENCH_REPO}"'

BOTO3_LAYER = "RUN pip install --no-cache-dir boto3\n"


def patch_dockerfile(text: str) -> str:
    text = text.replace(CLONE, f'git clone --depth=1 --branch {TAU2_REF} "${{TAU2_BENCH_REPO}}"')
    if "boto3" in text:
        return text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):  # before the first COPY keeps the task-specific layers last
        if line.startswith("COPY "):
            return "".join(lines[:i] + [BOTO3_LAYER] + lines[i:])
    return text.rstrip("\n") + "\n" + BOTO3_LAYER


def patch_task_toml(text: str) -> str:
    text = text.replace("${OPENAI_API_KEY}", "${OPENAI_API_KEY:-}")
    text = text.replace("${OPENAI_BASE_URL}", "${OPENAI_BASE_URL:-}")
    if "AWS_BEARER_TOKEN_BEDROCK" in text:
        return text
    pattern = re.compile(r"(\[verifier\][^\[]*?env = \{ )", flags=re.DOTALL)
    if not pattern.search(text):
        raise ValueError("[verifier] env table not found in task.toml")
    return pattern.sub(lambda m: m.group(1) + VERIFIER_VARS, text, count=1)


def main() -> int:
    root = Path(sys.argv[1])
    tasks = sorted(p.parent for p in root.glob("*/task.toml"))
    if not tasks:
        print(f"no tasks under {root}", file=sys.stderr)
        return 1
    for task in tasks:
        toml = task / "task.toml"
        toml.write_text(patch_task_toml(toml.read_text()))
        compose = task / "environment" / "docker-compose.yaml"
        compose.write_text(patch_compose(compose.read_text()))
        for dockerfile in (task / "environment" / "Dockerfile", task / "environment" / "runtime-server" / "Dockerfile"):
            dockerfile.write_text(patch_dockerfile(dockerfile.read_text()))
    print(f"patched {len(tasks)} tasks under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
