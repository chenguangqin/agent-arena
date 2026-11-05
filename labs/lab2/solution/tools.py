"""目标 1 · 自定义工具：一次调用找出所有重复扣款。

原始 API 需要模型自己翻页、处理偶发 503、按订单分组比对。这些是确定性的工作，
写成代码比让模型逐条调用更可靠，也省掉大量 token。
"""

from __future__ import annotations

import time
from typing import Any

from strands import tool

from labkit import Env

APPROVAL_THRESHOLD = 500.0

# 对本任务没有用、或者会造成错误操作的原始接口。
HIDDEN_RAW_TOOLS = ["refund_v1", "orders_list_v1", "ledger_export_v1", "reviews_list_v2", "audit_log_v2",
                    "orders_search_v2"]


def find_duplicates(call: Any, date: str) -> list[dict[str, Any]]:
    """``call(name, **args)`` calls a raw API endpoint. Shared with the MCP server (目标 2)."""
    payments, token = [], ""
    while True:
        for attempt in range(4):
            try:
                page = call("payments_list_v2", date=date, page_token=token)
                break
            except RuntimeError as exc:
                if "ERR_5031" not in str(exc) or attempt == 3:  # 503: transient, retry
                    raise
                time.sleep(0.2)
        payments.extend(page["data"])
        token = page["next_page_token"]
        if not token:
            break
    by_order: dict[str, list[dict[str, Any]]] = {}
    for p in payments:
        if p["status"] == "captured":
            by_order.setdefault(p["order_id"], []).append(p)
    rows = []
    for order_id, captures in sorted(by_order.items()):
        captures.sort(key=lambda p: p["created_at"])
        original, *extras = captures
        for p in extras:
            # 字段名写明用途，要执行的放在前面。早期版本先列"保留的支付"，模型因此退错了单。
            rows.append({"refund_payment_id": p["payment_id"], "refund_amount": p["amount"],
                         "needs_approval": p["amount"] > APPROVAL_THRESHOLD, "order_id": order_id,
                         "customer_id": p["customer_id"], "original_payment_do_not_refund": original["payment_id"]})
    return rows


def make_find_duplicate_charges(env: Env) -> Any:
    @tool
    def find_duplicate_charges(date: str) -> dict:
        """找出某天（YYYY-MM-DD）被重复扣款的所有订单。每条结果中 refund_payment_id / refund_amount
        是要退的那一笔；original_payment_do_not_refund 必须保留；needs_approval 为 true 表示超过 500 元，
        退款前要先申请审批。结果为空时先核对日期和年份。"""
        rows = find_duplicates(env.call, date)
        return {"date": date, "count": len(rows), "duplicates": rows}

    return find_duplicate_charges
