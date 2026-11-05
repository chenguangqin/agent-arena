"""Helpers shared by Lab2 features."""

from __future__ import annotations

import json
from typing import Any

from core.trace import result_text

# Display name -> raw API name, filled in by the curation feature (L2.1) when it renames tools.
ALIASES: dict[str, str] = {}


def canonical(name: str) -> str:
    """The raw API name behind a (possibly renamed) tool."""
    return ALIASES.get(name, name)


def parse_result(result: Any) -> Any:
    """Decode the JSON payload of a ToolResult; returns None when it is not JSON."""
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured.get("result", structured) if set(structured) == {"result"} else structured
        for block in result.get("content", []):
            if "json" in block:
                return block["json"]
    text = result_text(result)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def replace_text(result: dict[str, Any], text: str) -> dict[str, Any]:
    """Replace the payload. MCP results also carry ``structuredContent``; drop it so the
    original (unshaped) data cannot reach the model through another field."""
    shaped = {k: v for k, v in result.items() if k != "structuredContent"}
    return {**shaped, "content": [{"text": text}]}


def append_text(result: dict[str, Any], note: str) -> dict[str, Any]:
    return {**result, "content": [*result.get("content", []), {"text": note}]}
