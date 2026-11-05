"""Per-call token accounting.

Strands updates ``accumulated_usage`` only after ``AfterModelCallEvent`` fires,
so hooks cannot see the usage of the call that just finished. ``UsageTap``
wraps any model, reads the ``metadata.usage`` event at the end of each stream,
and reports it immediately. A call's input tokens equal the size of the
context the model saw, which is what Lab3 plots.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from strands.models import Model

UsageCallback = Callable[[dict[str, int]], None]


class UsageTap(Model):
    def __init__(self, inner: Model, on_usage: UsageCallback) -> None:
        self._inner = inner
        self._on_usage = on_usage
        self.last_usage: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:  # delegate provider-specific attributes
        return getattr(self._inner, name)

    def update_config(self, **model_config: Any) -> None:
        self._inner.update_config(**model_config)

    def get_config(self) -> Any:
        return self._inner.get_config()

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.structured_output(*args, **kwargs)

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self.last_usage = {}
        async for event in self._inner.stream(*args, **kwargs):
            usage = event.get("metadata", {}).get("usage") if isinstance(event, dict) else None
            if usage:
                self.last_usage = {
                    "input": int(usage.get("inputTokens", 0)),
                    "output": int(usage.get("outputTokens", 0)),
                    "cache_read": int(usage.get("cacheReadInputTokens", 0)),
                    "cache_write": int(usage.get("cacheWriteInputTokens", 0)),
                }
                self._on_usage(self.last_usage)
            yield event
