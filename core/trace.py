"""Event trace for teaching and review.

``Trace`` writes ``reports/trace.jsonl`` and fans events out to listeners (the
chat CLI prints them live). ``AgentTracer`` is the hook provider attached to
each agent — main agent, sub-agents, judges — labelled so events can be told
apart. Features record their own events with ``trace.emit("feature", ...)``.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    HookProvider,
    HookRegistry,
)

from core.budget import Budget
from core.usage import UsageTap

MAX_FIELD_CHARS = 2000

Listener = Callable[[dict[str, Any]], None]


def clip(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else f"{text[:limit]}…[{len(text) - limit} chars clipped]"


def result_text(result: Any) -> str:
    """Flatten a ToolResult's content blocks into text."""
    if not isinstance(result, dict):
        return str(result)
    parts = []
    for block in result.get("content", []):
        if "text" in block:
            parts.append(block["text"])
        elif "json" in block:
            parts.append(json.dumps(block["json"], ensure_ascii=False, default=str))
    return "\n".join(parts)


class Trace:
    def __init__(self, path: Path | None, budget: Budget, redact: tuple[str, ...] = ()) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.redact = tuple(v for v in redact if v)
        self.listeners: list[Listener] = []
        self.counts: Counter[str] = Counter()
        self.tool_calls = 0
        self.tool_errors = 0

    def subscribe(self, listener: Listener) -> None:
        self.listeners.append(listener)

    def emit(self, event: str, **fields: Any) -> None:
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        self.counts[event if event != "feature" else f"feature:{fields.get('name')}"] += 1
        line = json.dumps(record, ensure_ascii=False, default=str)
        for secret in self.redact:
            line = line.replace(secret, "[redacted]")
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        for listener in self.listeners:
            listener(json.loads(line))

    def feature(self, name: str, **fields: Any) -> None:
        """Record a harness feature event (offload, block, resume, skill load, ...)."""
        self.emit("feature", name=name, **fields)


class AgentTracer(HookProvider):
    """Per-agent hooks. Wrap the agent's model with :meth:`tap` so usage is exact."""

    def __init__(self, trace: Trace, label: str = "main") -> None:
        self.trace = trace
        self.label = label
        self.model_calls = 0
        self._tap: UsageTap | None = None
        self._started: float | None = None

    def tap(self, model: Any) -> UsageTap:
        self._tap = UsageTap(model, lambda usage: self.trace.budget.add(self.label, usage))
        return self._tap

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model)
        registry.add_callback(AfterModelCallEvent, self._after_model)
        registry.add_callback(AfterToolCallEvent, self._after_tool)
        registry.add_callback(AfterInvocationEvent, self._after_invocation)

    def _before_model(self, _: BeforeModelCallEvent) -> None:
        self._started = time.monotonic()

    def _after_model(self, event: AfterModelCallEvent) -> None:
        self.model_calls += 1
        usage = dict(self._tap.last_usage) if self._tap else {}
        stop = event.stop_response
        text = ""
        if stop is not None:
            text = " ".join(
                b["text"] for b in stop.message.get("content", []) if isinstance(b.get("text"), str)
            )
        context = usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0)
        estimated = None
        if not usage and event.exception is not None:
            # A rejected call (e.g. "prompt is too long") reports no usage; estimate what was sent
            # so the peak is not understated. Rough rule: ~3 characters per token.
            chars = len(json.dumps(event.agent.messages, ensure_ascii=False, default=str))
            estimated = chars // 3
            self.trace.budget.peak_context_tokens = max(self.trace.budget.peak_context_tokens, estimated)
        self.trace.emit(
            "model_call",
            agent=self.label,
            index=self.model_calls,
            stop_reason=getattr(stop, "stop_reason", None),
            context_tokens=context,
            context_tokens_estimated=estimated,
            output_tokens=usage.get("output"),
            cache_read_tokens=usage.get("cache_read"),
            messages=len(event.agent.messages),
            text=clip(text, 500) if text else None,
            error=repr(event.exception) if event.exception else None,
            seconds=round(time.monotonic() - self._started, 3) if self._started else None,
        )

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        status = event.result.get("status") if isinstance(event.result, dict) else None
        if event.retry:  # discarded attempt; the tool runs again and is traced again
            status = "retried"
        else:
            self.trace.tool_calls += 1
            if status == "error":
                self.trace.tool_errors += 1
        output = result_text(event.result)
        self.trace.emit(
            "tool_call",
            agent=self.label,
            name=event.tool_use.get("name"),
            input=clip(event.tool_use.get("input"), 800),
            status=status,
            result_chars=len(output),
            result=clip(output, 800),
            cancelled=event.cancel_message,
            seconds=round(event.duration, 3) if event.duration is not None else None,
        )

    def _after_invocation(self, event: AfterInvocationEvent) -> None:
        stop_reason = getattr(event.result, "stop_reason", None) if event.result else None
        self.trace.emit("invocation_end", agent=self.label, stop_reason=stop_reason)
