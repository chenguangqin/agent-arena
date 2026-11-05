"""Helpers shared by Lab5 features."""

from __future__ import annotations

from typing import Any

from core.lab import RunContext


def sub_model_id(ctx: RunContext) -> str | None:
    """L5.9: model for helper agents; ``same`` reuses the main model."""
    value = ctx.config.get("orchestration", "sub_model")
    return None if value == "same" else value


def append_to_result(result: dict[str, Any], note: str) -> dict[str, Any]:
    """Return a copy of a ToolResult with an extra text block (seen by the model)."""
    content = list(result.get("content", [])) + [{"text": note}]
    return {**result, "content": content}


def stop_loop(invocation_state: dict[str, Any]) -> None:
    """Ask the event loop to end after the current tool batch."""
    invocation_state.setdefault("request_state", {})["stop_event_loop"] = True
