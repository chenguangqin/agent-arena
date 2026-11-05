"""Lab3 · Context engineering (M3).

Scenario: ``lab3-checkout-incident`` — find a config regression in ~300k log
lines. See docs/LABS_SPEC.md §5.
"""

from __future__ import annotations

from pathlib import Path

from core.config import IMPL, OFF, ON, Config
from core.lab import Feature, Lab
from lab3.features import cache, compaction, inject, notes, offload, prompt, retrieval

SCHEMA = {
    "context": {
        "structured_prompt": (OFF, ON),  # L3.1
        "offload": (OFF, IMPL),  # L3.2 hooks = own offloader, sdk = ContextOffloader
        "offload_threshold": (8000, int),  # characters
        "retrieval": (OFF, ON),  # L3.3 query_logs + BM25 runbook search
        "compaction": (OFF, IMPL),  # L3.4 hooks = template compactor, sdk = SummarizingConversationManager
        "compaction_tokens": (40000, int),  # compact when the projected context exceeds this
        "notes": (OFF, ON),  # L3.5
        "inject": (OFF, ON),  # L3.6 ContextInjector status block
        "cache": (OFF, ON),  # L3.7 prompt caching
    },
}

PROMPT = """你是在线商店的值班 SRE Agent，在隔离的任务环境中工作。
使用可用的工具排查并解决问题，直接动手处理，而不是只给建议。"""


def system_prompt(config: Config) -> str:
    return PROMPT


# Order matters: the structured prompt replaces the base prompt before other features append guidance.
FEATURES = (
    Feature("context", "structured_prompt", prompt.install, "L3.1 结构化 system prompt"),
    Feature("context", "offload", offload.install, "L3.2 大结果卸载"),
    Feature("context", "retrieval", retrieval.install, "L3.3 按需检索"),
    Feature("context", "notes", notes.install, "L3.5 结构化笔记"),
    Feature("context", "compaction", compaction.install, "L3.4 Compaction"),
    Feature("context", "inject", inject.install, "L3.6 即时注入"),
    Feature("context", "cache", cache.install, "L3.7 Prompt caching"),
)

LAB = Lab(
    name="lab3",
    title="Lab3 · Context engineering (M3)",
    root=Path(__file__).resolve().parent,
    schema=SCHEMA,
    scenario="lab3-checkout-incident",
    system_prompt=system_prompt,
    features=FEATURES,
)
