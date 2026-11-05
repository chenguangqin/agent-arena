"""Records what the agent did: model calls, tool calls, and your own events.

The chat prints events as they happen; every event is also appended to
``reports/trace-<时间>.jsonl``（每次运行一个文件）。 From your code you can record an event with::

    from labkit import trace
    trace.event("policy_block", tool="refund_v1", reason="deprecated")
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from strands.hooks import AfterModelCallEvent, AfterToolCallEvent, HookProvider, HookRegistry

from labkit.usage import UsageTap

_state: dict[str, Any] = {"path": None, "echo": False, "redact": (), "totals": {}, "by_agent": {}}
_lock = threading.RLock()  # workflow nodes run in parallel threads


def configure(path: Path | None, echo: bool, redact: tuple[str, ...] = ()) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    _state.update(path=path, echo=echo, redact=tuple(r for r in redact if r))
    reset_totals()


def reset_totals() -> None:
    _state["totals"] = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                        "peak_context_tokens": 0, "tool_calls": 0, "tool_errors": 0}
    _state["by_agent"] = {}
    _state["last_usage"] = {}


def totals() -> dict[str, Any]:
    """Run-wide totals, plus per-agent input tokens and peak context (``by_agent``)."""
    with _lock:
        return {**_state["totals"], "by_agent": {k: dict(v) for k, v in _state["by_agent"].items()}}


def event(name: str, **fields: Any) -> None:
    """Record a custom event (shown in chat as ◆ name)."""
    _write({"event": name, **fields})
    if _state["echo"]:
        print(f"  ◆ {name}: {_short(fields, 240)}")


def tap(model: Any, label: str = "main") -> UsageTap:
    """Wrap a model so its token usage is counted under ``label`` (one label per agent role)."""
    tapped = UsageTap(model, lambda usage: _on_usage(label, usage))
    return tapped


def _on_usage(label: str, usage: dict[str, int]) -> None:
    context = usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0)
    with _lock:
        t = _state["totals"]
        t["model_calls"] += 1
        t["input_tokens"] += usage.get("input", 0)
        t["output_tokens"] += usage.get("output", 0)
        t["cache_read_tokens"] += usage.get("cache_read", 0)
        t["peak_context_tokens"] = max(t["peak_context_tokens"], context)
        agent = _state["by_agent"].setdefault(label, {"model_calls": 0, "input_tokens": 0, "peak_context_tokens": 0})
        agent["model_calls"] += 1
        agent["input_tokens"] += usage.get("input", 0)
        agent["peak_context_tokens"] = max(agent["peak_context_tokens"], context)
        _state["last_usage"][label] = {"context": context, "output": usage.get("output", 0)}


class Tracer(HookProvider):
    """Hook provider that records an agent's model and tool calls.

    labkit attaches it to the agent returned by build_agent(). For agents you create
    yourself (workflow nodes, the orchestrator), pass ``hooks=[trace.Tracer("label")]``
    and build their model with ``build_model(label="label")``.
    """

    def __init__(self, label: str = "main") -> None:
        self.label = label
        self._tag = "" if label == "main" else f"[{label}] "

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterModelCallEvent, self._after_model)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_model(self, e: AfterModelCallEvent) -> None:
        usage = (_state.get("last_usage") or {}).get(self.label, {})
        context = usage.get("context", 0)
        stop = getattr(e.stop_response, "stop_reason", None)
        _write({"event": "model_call", "agent": self.label, "context_tokens": context,
                "output_tokens": usage.get("output"), "stop_reason": stop,
                "error": repr(e.exception) if e.exception else None})
        if _state["echo"]:
            print(f"  {self._tag}· model: context {context} tokens, stop={stop}")

    def _after_tool(self, e: AfterToolCallEvent) -> None:
        status = "retried" if e.retry else e.result.get("status")
        text = _result_text(e.result)
        if not e.retry:
            with _lock:
                _state["totals"]["tool_calls"] += 1
                _state["totals"]["tool_errors"] += status == "error"
        _write({"event": "tool_call", "agent": self.label, "name": e.tool_use.get("name"),
                "input": e.tool_use.get("input"), "status": status, "result_chars": len(text),
                "result": text[:1500]})
        if _state["echo"]:
            mark = {"success": "✓", "error": "✗", "retried": "↻"}.get(status, "?")
            print(f"  {self._tag}{mark} {e.tool_use.get('name')}({_short(e.tool_use.get('input'), 100)}) "
                  f"→ {len(text)} chars")


def _result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)
    parts = []
    for block in result.get("content", []):
        if "text" in block:
            parts.append(block["text"])
        elif "json" in block:
            parts.append(json.dumps(block["json"], ensure_ascii=False))
    return "\n".join(parts)


def _write(record: dict[str, Any]) -> None:
    path = _state["path"]
    if path is None:
        return
    line = json.dumps({"ts": round(time.time(), 3), **record}, ensure_ascii=False, default=str)
    for secret in _state["redact"]:
        line = line.replace(secret, "[redacted]")
    with _lock, path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
