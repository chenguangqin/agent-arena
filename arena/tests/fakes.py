"""Scripted Strands model for tests: replays a fixed list of assistant turns."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from strands.models import Model


def text(content: str) -> dict[str, Any]:
    return {"text": content}


def tool(name: str, tool_use_id: str, **arguments: Any) -> dict[str, Any]:
    return {"tool": name, "id": tool_use_id, "input": arguments}


class ScriptedModel(Model):
    """Each turn is a list of blocks built with :func:`text` / :func:`tool`."""

    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.config: dict[str, Any] = {"model_id": "scripted"}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> Any:
        return self.config

    async def structured_output(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncIterator[Any]:
        self.requests.append(
            {"messages": list(messages), "tools": [t["name"] for t in tool_specs or []], "system": system_prompt}
        )
        if not self.turns:
            raise AssertionError("ScriptedModel ran out of turns")
        blocks = self.turns.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        stop = "end_turn"
        for block in blocks:
            if "text" in block:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
                yield {"contentBlockStop": {}}
            else:
                stop = "tool_use"
                start = {"toolUse": {"name": block["tool"], "toolUseId": block["id"]}}
                yield {"contentBlockStart": {"start": start}}
                delta = {"toolUse": {"input": json.dumps(block["input"])}}
                yield {"contentBlockDelta": {"delta": delta}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": stop}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
                "metrics": {"latencyMs": 1},
            }
        }
