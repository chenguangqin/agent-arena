"""Helpers shared by Lab4 features."""

from __future__ import annotations

import re
from typing import Any

from core.lab import RunContext


def current_ticket(ctx: RunContext) -> dict[str, Any]:
    """The ticket the driver is working on (set before each fresh agent is built)."""
    return ctx.summary.get("current_ticket") or {}


def state_dir(ctx: RunContext, rel: str):
    from lab4 import LAB

    path = LAB.state_dir(ctx, rel)
    path.mkdir(parents=True, exist_ok=True)
    return path


def tokens(text: str) -> set[str]:
    """Crude keyword set for matching: ids, latin words, and Chinese character bigrams."""
    ids = set(re.findall(r"[A-Z]+-?\d+|PG-[A-Z]", text))
    words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", text)}
    han = re.sub(r"[^一-鿿]", "", text)
    return ids | words | {han[i:i + 2] for i in range(len(han) - 1)}


def prepend_to_first_message(event: Any, text: str) -> None:
    """Add context in front of the user's first message of this invocation (BeforeInvocationEvent)."""
    if not event.messages:
        return
    first = event.messages[0]
    event.messages[0] = {**first, "content": [{"text": text}, *first.get("content", [])]}
