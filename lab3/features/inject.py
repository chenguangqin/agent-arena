"""L3.6 · Just-in-time context injection.

``ContextInjector`` adds a short status block before every model call — the
goal, the remaining token budget, the current context size, and the latest
notes — without writing it into history, so it never accumulates.
"""

from __future__ import annotations

from typing import Any

from core.lab import AgentSpec, RunContext
from lab3.features.compaction import estimate_tokens
from lab3.features.notes import notes_path


def install(spec: AgentSpec, ctx: RunContext) -> None:
    from strands.vended_plugins.context_injector import ContextInjector

    calls = {"n": 0}

    def render(c: Any) -> str:
        calls["n"] += 1
        remaining = ctx.budget.remaining
        context = estimate_tokens(c.messages)
        notes = notes_path(ctx)
        recent = notes.read_text(encoding="utf-8").splitlines()[-5:] if notes.exists() else []
        if calls["n"] == 1 or calls["n"] % 5 == 0:
            ctx.trace.feature("inject", call=calls["n"], context_tokens_est=context, remaining=remaining)
        return (
            "<status>\n"
            f"目标：找到根因并修复，用大购物车重放验证，给出带证据的摘要。\n"
            f"当前上下文约 {context} tokens；剩余预算 {remaining if remaining is not None else '不限'} tokens。"
            "上下文偏大时避免再读取大段原始日志。\n"
            + ("最近笔记：\n" + "\n".join(recent) + "\n" if recent else "")
            + "</status>"
        )

    spec.plugins.append(ContextInjector(render, trigger="everyTurn"))
