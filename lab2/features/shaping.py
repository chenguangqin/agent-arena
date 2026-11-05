"""L2.3 · Result shaping.

Raw payment records carry 40+ fields. An ``AfterToolCallEvent`` hook keeps
only the fields the task needs and caps the length, so each page costs a
fraction of the context. The trace records the size before and after.
"""

from __future__ import annotations

import json
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from core.lab import AgentSpec, RunContext
from core.trace import result_text
from lab2.features.common import canonical, parse_result, replace_text

KEEP = {
    "payment": ("payment_id", "order_id", "customer_id", "amount", "status", "refunded_amount", "created_at"),
    "order": ("id", "customer_id", "status", "total", "created_at", "notes"),
}
MAX_CHARS = 6000


def _shape(value: Any) -> Any:
    if isinstance(value, list):
        return [_shape(v) for v in value]
    if isinstance(value, dict):
        if "payment_id" in value and "gateway_txn_id" in value:
            return {k: value[k] for k in KEEP["payment"] if k in value}
        if "total" in value and "shipping" in value:
            return {k: value[k] for k in KEEP["order"] if k in value}
        return {k: _shape(v) for k, v in value.items()}
    return value


class ResultShaper(HookProvider):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        if event.result.get("status") == "error":
            return
        payload = parse_result(event.result)
        if payload is None:
            return
        before = len(result_text(event.result))
        text = json.dumps(_shape(payload), ensure_ascii=False, separators=(",", ":"))
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + f"…[truncated {len(text) - MAX_CHARS} chars; narrow the query]"
        if len(text) < before:
            event.result = replace_text(event.result, text)
            self.ctx.trace.feature("result_shaped", tool=canonical(event.tool_use.get("name", "")),
                                   before_chars=before, after_chars=len(text))


def install(spec: AgentSpec, ctx: RunContext) -> None:
    spec.hooks.append(ResultShaper(ctx))
