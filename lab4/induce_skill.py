"""L4.4 · Induce a candidate skill from a successful ticket's trajectory (offline).

Reads ``trace.jsonl`` from a run, takes the tool calls one ticket's agent made,
and writes a candidate ``SKILL.md`` (frontmatter + numbered steps). Candidates
go to ``skills/candidates/`` and are not loaded until a person reviews them and
moves them up a level.

    uv run python -m lab4.induce_skill --trace reports/<run>/trace.jsonl --ticket T-802 \\
        --name duplicate-charge --description "重复扣款类工单"

Only induce from tickets whose outcome you have verified; a skill induced from
an accidental success teaches the wrong procedure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CANDIDATES = Path(__file__).resolve().parent / "skills" / "candidates"
SKIP = {"next_ticket", "load_skill", "skills", "recall", "remember", "search_memory", "add_memory"}


def steps_from_trace(trace: Path, ticket: str) -> list[str]:
    steps, last = [], None
    for line in trace.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") != "tool_call" or event.get("agent") != ticket or event.get("status") != "success":
            continue
        name = event["name"]
        if name in SKIP or name == last:
            continue
        try:
            keys = sorted(json.loads(event.get("input") or "{}"))
        except json.JSONDecodeError:
            keys = []
        steps.append(f"调用 {name}" + (f"（参数：{', '.join(keys)}）" if keys else ""))
        last = name
    return steps


def render(name: str, description: str, ticket: str, steps: list[str]) -> str:
    body = "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1))
    return (f"---\nname: {name}\ndescription: {description}\n---\n"
            f"# {name}（候选，归纳自工单 {ticket} 的成功轨迹，待人工审核）\n\n{body}\n\n"
            "审核要点：步骤是否只适用于这一张工单；金额阈值、审批、升级等条件是否需要补充为判断分支。\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--ticket", required=True, help="agent label in the trace, e.g. T-802")
    parser.add_argument("--name", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--out", type=Path, default=CANDIDATES)
    args = parser.parse_args(argv)
    steps = steps_from_trace(args.trace, args.ticket)
    if not steps:
        parser.error(f"no successful tool calls for {args.ticket} in {args.trace}")
    target = args.out / args.name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(args.name, args.description, args.ticket, steps), encoding="utf-8")
    print(f"candidate skill written to {target} ({len(steps)} steps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
