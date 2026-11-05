"""Deterministic completion checks, run after the workflow finishes.

They query the environment the way a test suite would; the orchestrator gets the list
of problems and plans a follow-up workflow. Notifications cannot be read back from the
store API, so they are checked against what the harness observed (``NotifyLedger``).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from labkit import Env, trace

REPORT_SECTIONS = ("时间线", "影响范围", "处理结果")


class NotifyLedger(HookProvider):
    """Shared by every node: records notifications and blocks a second one to the same customer."""

    def __init__(self) -> None:
        self.sent: Counter[str] = Counter()

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool)

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") != "notify_customer":
            return
        customer = (event.tool_use.get("input") or {}).get("customer_id", "")
        if self.sent[customer]:
            trace.event("duplicate_notification_blocked", customer_id=customer)
            event.cancel_tool = f"客户 {customer} 已经通知过，每位客户只通知一次。"
            return
        self.sent[customer] += 1


class RefundGuard(HookProvider):
    """Shared by every node: a refund is allowed only after that order's replay failed as sold out.

    Without it, a workflow that forgets "fix the rule before replaying" makes every replay
    fail with ERR_NEGATIVE_TOTAL, and workers refund all orders. The guard turns that
    missing dependency into a failed step, which goes back to the orchestrator.
    """

    def __init__(self) -> None:
        self.sold_out: set[str] = set()  # order ids whose replay failed with OUT_OF_STOCK
        self.payment_order: dict[str, str] = {}  # learned from get_order results

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        name, args = event.tool_use.get("name"), event.tool_use.get("input") or {}
        text = "\n".join(b.get("text", "") for b in event.result.get("content", []))
        if name == "get_order" and event.result.get("status") != "error":
            match = re.search(r'"payment_id":\s*"([^"]+)"', text)
            if match:
                self.payment_order[match.group(1)] = args.get("order_id", "")
        if name == "replay_order" and "OUT_OF_STOCK" in text:
            self.sold_out.add(args.get("order_id", ""))
        if name == "replay_order" and "ERR_NEGATIVE_TOTAL" in text:
            note = ("[harness] 这是折扣规则尚未修复导致的，不是订单本身无法重放。不要退款；"
                    "回复的第一行写 FAILED: 折扣规则未修复，重放无法进行。")
            event.result = {**event.result, "content": [*event.result.get("content", []), {"text": note}]}

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") != "issue_refund":
            return
        payment = (event.tool_use.get("input") or {}).get("payment_id", "")
        order = self.payment_order.get(payment)
        if order in self.sold_out:
            return
        trace.event("refund_blocked", payment_id=payment, order_id=order)
        event.cancel_tool = (f"只有重放因库存售罄（OUT_OF_STOCK）失败的订单才能退款；{payment} 对应的订单"
                             f"{'未知（先 get_order）' if order is None else f' {order} 没有这样的重放记录'}。")


def affected_orders(env: Env) -> list[dict[str, Any]]:
    orders, page = [], 1
    while True:
        result = env.call("list_failed_orders", page=page)
        orders.extend(result["orders"])
        if not result["has_more"]:
            return orders
        page += 1


def completion_problems(env: Env, ledger: NotifyLedger) -> list[str]:
    problems = []
    broken = [r["id"] for r in env.call("list_discount_rules") if r["max_discount_pct"] > 90]
    if broken:
        problems.append(f"折扣规则 {', '.join(broken)} 仍配置错误")
    orders = affected_orders(env)
    unresolved = [o["id"] for o in orders if o["status"] in ("failed", "replay_failed")]
    if unresolved:
        problems.append(f"还有 {len(unresolved)} 个受影响订单既没有重放成功也没有退款：{', '.join(unresolved[:10])}")
    customers = sorted({o["customer_id"] for o in orders})
    missing = [c for c in customers if not ledger.sent[c]]
    if missing:
        problems.append(f"还有 {len(missing)} 位受影响客户没有收到通知：{', '.join(missing[:10])}")
    open_tickets = [t["id"] for t in env.call("list_tickets", status="open") if "大促" in t["subject"]]
    if open_tickets:
        problems.append(f"相关工单仍未关闭：{', '.join(open_tickets)}")
    report = env.call("get_report")
    if not report["exists"]:
        problems.append("还没有保存复盘报告")
    else:
        absent = [s for s in REPORT_SECTIONS if s not in report["markdown"]]
        if absent:
            problems.append(f"复盘报告缺少章节：{'、'.join(absent)}")
    return problems


class TicketGuard(HookProvider):
    """Shared by every node: only tickets related to the incident may be closed.

    A ticket counts as related when its customer has an affected (failed) order. Closing an
    unrelated ticket cannot be detected afterwards (there is no API to reopen it), so it is
    prevented up front, like refunds.
    """

    def __init__(self) -> None:
        self.ticket_customer: dict[str, str] = {}  # learned from list_tickets results
        self.affected_customers: set[str] = set()  # learned from failed-order listings

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        if event.result.get("status") == "error":
            return
        name = event.tool_use.get("name")
        text = "\n".join(b.get("text", "") for b in event.result.get("content", []))
        if name == "list_tickets":
            for tid, cid in re.findall(r'"id":\s*"(T-\d+)",\s*"customer_id":\s*"([^"]+)"', text):
                self.ticket_customer[tid] = cid
        if name in ("list_failed_orders", "get_order"):
            self.affected_customers.update(re.findall(r'"customer_id":\s*"([^"]+)"', text))

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") != "close_ticket":
            return
        ticket = (event.tool_use.get("input") or {}).get("ticket_id", "")
        customer = self.ticket_customer.get(ticket)
        if customer is not None and customer in self.affected_customers:
            return
        trace.event("ticket_close_blocked", ticket_id=ticket, customer_id=customer)
        event.cancel_tool = (f"工单 {ticket} 与本次事故无关或尚未确认：只能关闭受影响客户的工单"
                             "（先 list_tickets 和 list_failed_orders 确认）。")
