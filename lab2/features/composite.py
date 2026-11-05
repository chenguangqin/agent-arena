"""L2.2 · Composite tool.

``find_duplicate_charges`` does in code what the model would otherwise do in
many calls: page through every payment record, retry transient errors, group
captures by order, and return only the orders captured more than once.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from strands import tool

from core.lab import AgentSpec, RunContext

APPROVAL_THRESHOLD = 500.0


def find_duplicates(call: Callable[..., Any], date: str, retries: int = 3) -> list[dict]:
    """``call(tool_name, **arguments)`` invokes a raw API tool (harness or MCP server side)."""
    payments, token = [], ""
    while True:
        for attempt in range(retries + 1):
            try:
                page = call("payments_list_v2", date=date, page_token=token)
                break
            except RuntimeError as exc:
                if "ERR_5031" not in str(exc) or attempt == retries:
                    raise
                time.sleep(0.2)
        payments.extend(page["data"])
        token = page["next_page_token"]
        if not token:
            break
    by_order: dict[str, list[dict]] = {}
    for p in payments:
        if p["status"] == "captured":
            by_order.setdefault(p["order_id"], []).append(p)
    rows = []
    for order_id, captures in sorted(by_order.items()):
        if len(captures) < 2:
            continue
        captures.sort(key=lambda p: p["created_at"])
        keep, *extra = captures
        for p in extra:
            # Field names say what to do; the action comes first. An earlier version listed
            # keep_payment_id before refund_payment_id and the model refunded the wrong one.
            rows.append({"refund_payment_id": p["payment_id"], "refund_amount": p["amount"],
                         "needs_approval": p["amount"] > APPROVAL_THRESHOLD, "order_id": order_id,
                         "customer_id": p["customer_id"], "original_payment_do_not_refund": keep["payment_id"]})
    return rows


def install(spec: AgentSpec, ctx: RunContext) -> None:
    @tool
    def find_duplicate_charges(date: str) -> dict:
        """Find every order captured more than once on a date (YYYY-MM-DD). Pages through all
        payments and retries transient errors. For each duplicate: refund_payment_id and
        refund_amount are what to refund; original_payment_do_not_refund must be kept;
        needs_approval is true above 500."""
        rows = find_duplicates(ctx.call_env, date)
        ctx.trace.feature("composite_tool", tool="find_duplicate_charges", duplicates=len(rows))
        return {"date": date, "duplicates": rows, "count": len(rows)}

    spec.tools.append(find_duplicate_charges)
