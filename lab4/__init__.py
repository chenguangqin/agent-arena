"""Lab4 · Memory and skills (M4).

Scenario: ``lab4-ticket-queue`` — 10 tickets, each handled by a fresh agent
context; three long SOPs; facts and QA rules that only help if remembered.
See docs/LABS_SPEC.md §6.
"""

from __future__ import annotations

from pathlib import Path

from core.config import IMPL, OFF, ON, Config
from core.lab import Feature, Lab
from lab4.driver import driver
from lab4.features import lessons, memory, skills

SCHEMA = {
    "skills": {
        "mode": (OFF, IMPL),  # L4.1 hooks = load_skill, sdk = AgentSkills
        "sop_in_prompt": (OFF, ON),  # L4.1 control group: all SOPs in the system prompt
    },
    "memory": {
        "facts": (OFF, IMPL),  # L4.2 hooks = facts.md + remember/recall, sdk = MemoryManager
        "lessons": (OFF, ON),  # L4.3 rules distilled from QA feedback
    },
}

PROMPT = """你是在线商店的客服 Agent，在隔离的任务环境中工作，每次处理一张工单。
使用可用的工具核实情况并直接处理，而不是只给建议。公司政策可以用 list_policies / read_policy 查阅。"""


def system_prompt(config: Config) -> str:
    return PROMPT


FEATURES = (
    Feature("skills", "sop_in_prompt", skills.install_sop_in_prompt, "L4.1 对照组：SOP 全文放进 prompt"),
    Feature("skills", "mode", skills.install_skills, "L4.1 SOP 写成 skill"),
    Feature("memory", "facts", memory.install_facts, "L4.2 事实记忆"),
    Feature("memory", "lessons", lessons.install, "L4.3 反馈记忆"),
)

LAB = Lab(
    name="lab4",
    title="Lab4 · Memory 与 Skills (M4)",
    root=Path(__file__).resolve().parent,
    schema=SCHEMA,
    scenario="lab4-ticket-queue",
    system_prompt=system_prompt,
    features=FEATURES,
    driver=driver,
    state_paths=("memory", "skills"),
)
