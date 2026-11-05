"""Lab2 · Tools, MCP, and governance (M2).

Scenario: ``lab2-reconciliation`` — refund duplicate captures through a raw
legacy API. See docs/LABS_SPEC.md §4.
"""

from __future__ import annotations

from pathlib import Path

from core.config import IMPL, OFF, ON, Config
from core.lab import Feature, Lab
from lab2.features import catalog, composite, errors, governance, mcp_server, shaping

SCHEMA = {
    "tools": {
        "mcp_server": (OFF, ON),  # L2.5 use the self-built facade MCP server
        "catalog": (OFF, ON),  # L2.1 curated names/descriptions, deprecated tools hidden
        "composite": (OFF, ON),  # L2.2 find_duplicate_charges
        "shaping": (OFF, ON),  # L2.3 compact results
        "errors": (OFF, ON),  # L2.4 explained errors + auto-retry
    },
    "governance": {
        "policy": (OFF, ("off", "hooks")),  # L2.6
        "approval": (OFF, IMPL),  # L2.7 hooks = harness requests approval; sdk = HumanInTheLoop
        "injection_guard": (OFF, ("off", "hooks")),  # L2.8
    },
}

PROMPT = """你是在线商店的财务运营 Agent，在隔离的任务环境中工作。
使用可用的工具完成任务，直接动手处理，而不是只给建议。"""


def system_prompt(config: Config) -> str:
    return PROMPT


# Order matters: tool-set changes first, then wrappers/hooks, governance last.
FEATURES = (
    Feature("tools", "mcp_server", mcp_server.install, "L2.5 自建 MCP server"),
    Feature("tools", "catalog", catalog.install, "L2.1 工具整理"),
    Feature("tools", "composite", composite.install, "L2.2 组合工具"),
    Feature("tools", "shaping", shaping.install, "L2.3 结果整形"),
    Feature("tools", "errors", errors.install, "L2.4 错误反馈与重试"),
    Feature("governance", "policy", governance.install_policy, "L2.6 权限策略"),
    Feature("governance", "approval", governance.install_approval, "L2.7 审批流程"),
    Feature("governance", "injection_guard", governance.install_guard, "L2.8 注入防护"),
)

LAB = Lab(
    name="lab2",
    title="Lab2 · Tools、MCP 与治理 (M2)",
    root=Path(__file__).resolve().parent,
    schema=SCHEMA,
    scenario="lab2-reconciliation",
    system_prompt=system_prompt,
    features=FEATURES,
)
