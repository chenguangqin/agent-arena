"""L5.10 · Output-limit recovery.

When one response exceeds ``max_tokens`` (typically: many tool calls at once,
such as notifying 30 customers in a single turn), Strands raises
``MaxTokensReachedException`` with the partial message already in history.
Without recovery the run ends there. With recovery, the driver continues the
same conversation and asks the model to work in smaller batches.
"""

from __future__ import annotations

from typing import Any

from core.lab import Lab, RunContext, build_agent

CONTINUE = (
    "[harness] 你上一条回复超出了单次输出上限，被截断了，其中的工具调用没有执行。"
    "继续完成任务，但每轮只发起少量工具调用（例如不超过 8 个），分批处理。"
)


def driver(lab: Lab, ctx: RunContext) -> Any:
    """Lab5 driver: the default single invocation, plus recovery when enabled."""
    from strands.types.agent import Limits
    from strands.types.exceptions import MaxTokensReachedException

    agent, spec = build_agent(lab, ctx)
    message = ctx.instruction
    for transform in spec.instruction_transforms:
        message = transform(message)
    limits = Limits(turns=ctx.config.get("limits", "max_turns"))
    retries = ctx.config.get("guard", "max_recoveries") if ctx.config.enabled("guard", "output_recovery") else 0
    for attempt in range(retries + 1):
        try:
            return agent(message, limits=limits)
        except MaxTokensReachedException:
            if attempt == retries or ctx.budget.exhausted():
                raise
            ctx.trace.feature("output_recovery", attempt=attempt + 1)
            message = CONTINUE
    raise AssertionError("unreachable")
