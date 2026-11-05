"""Lab5 · Dynamic workflows (M5): planning, completion gate, sub-agents, stall detection.

Scenario: ``lab5-promo-incident`` — six requirements, a replay outage, sold-out
SKUs, paginated orders. See docs/LABS_SPEC.md §7.
"""

from __future__ import annotations

from pathlib import Path

from core.config import IMPL, OFF, ON, Config
from core.lab import Feature, Lab
from lab5.features import completion, fanout, planner, recovery, stall, todo, verifier

SCHEMA = {
    "planning": {
        "todo": (OFF, ON),  # L5.1
        "plan_first": (OFF, ON),  # L5.2 plan-then-execute
    },
    "completion": {
        "finish_tool": (OFF, ON),  # L5.3
        "checks": (OFF, ON),  # L5.4
        "judge": (OFF, IMPL),  # L5.5 (hooks = hand-built, sdk = GoalLoop)
        "max_rounds": (3, int),
    },
    "orchestration": {
        "verifier": (OFF, ON),  # L5.6
        "fanout": (OFF, ON),  # L5.7
        "batch_size": (8, int),
        "max_workers": (4, int),
        "sub_model": ("same", str),  # L5.9
    },
    "guard": {
        "stall": (OFF, ("off", "hooks")),  # L5.8
        "threshold": (3, int),
        "output_recovery": (OFF, ON),  # L5.10
        "max_recoveries": (3, int),
    },
}

PROMPT = """你是在线商店的运维 Agent，在隔离的任务环境中工作。
使用可用的工具检查环境并完成任务，直接动手处理，而不是只给建议。"""


def system_prompt(config: Config) -> str:
    return PROMPT


FEATURES = (
    Feature("planning", "todo", todo.install, "L5.1 todo 与重新规划"),
    Feature("planning", "plan_first", planner.install, "L5.2 plan-then-execute"),
    Feature("completion", "finish_tool", completion.install_finish, "L5.3 finish 工具"),
    Feature("completion", "checks", completion.install_checks, "L5.4 确定性完成检查"),
    Feature("completion", "judge", completion.install_judge, "L5.5 独立判定器"),
    Feature("orchestration", "verifier", verifier.install, "L5.6 verifier sub-agent"),
    Feature("orchestration", "fanout", fanout.install, "L5.7 扇出"),
    Feature("guard", "stall", stall.install, "L5.8 停滞检测"),
    # L5.10 output_recovery is applied by the driver (recovery.driver), not installed on the agent.
)

LAB = Lab(
    name="lab5",
    title="Lab5 · 动态工作流 (M5)",
    root=Path(__file__).resolve().parent,
    schema=SCHEMA,
    scenario="lab5-promo-incident",
    system_prompt=system_prompt,
    features=FEATURES,
    driver=recovery.driver,
)
