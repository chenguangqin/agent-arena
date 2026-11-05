"""Lab5 scenario: promotion incident with six requirements and dynamic conditions.

A discount rule misconfigured at 20:05 (``max_discount_pct`` 150 instead of 50)
made checkout compute negative totals, so ~40 orders failed *after* payment was
captured. The task bundles six requirements; dynamics force plans to change:

* replaying an order fails until the rule is fixed;
* the replay service returns 503 for a stretch of calls;
* some SKUs are sold out, so those orders must be refunded instead of replayed;
* several customers have more than one failed order but must be notified once;
* failed orders are paginated.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

ID = "lab5-promo-incident"
INSTRUCTION = (
    "大促事故处理。今晚 20:00 之后 checkout 大量失败，请完成以下全部工作：\n"
    "1. 找到导致失败的折扣规则；\n"
    "2. 修复该规则的配置；\n"
    "3. 找出所有受影响（失败但已扣款）的订单，逐一重放下单；\n"
    "4. 重放失败的订单（例如库存已售罄）改为全额退款，同一订单不能既重放成功又退款；\n"
    "5. 通知每一位受影响的客户处理结果，每位客户只通知一次；\n"
    "6. 关闭与本次事故相关的工单，并保存一份复盘报告，报告需包含"
    "「时间线」「影响范围」「处理结果」三个章节。"
)

BAD_RULE = "PROMO-1111-STACK"
PAGE_SIZE = 15
SOLD_OUT_SKUS = ("SKU-AIRFRYER-X", "SKU-HEADPHONE-Q")
REPORT_SECTIONS = ("时间线", "影响范围", "处理结果")


@dataclass
class PromoEnv:
    seed: int = 0
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    report: str | None = None
    replay_calls: int = 0
    # Replay calls with these 1-based indices return 503 (after the rule is fixed).
    outage: range = range(4, 12)

    # ---- diagnosis -------------------------------------------------------
    def list_alerts(self) -> list[dict[str, Any]]:
        return [
            {"time": "20:07", "service": "checkout", "alert": "error rate > 25%"},
            {"time": "20:09", "service": "payment", "alert": "captures without completed orders"},
            {"time": "20:15", "service": "recommendation", "alert": "latency p99 > 2s (unrelated)"},
        ]

    def checkout_error_summary(self, since: str = "20:00", until: str = "21:00") -> list[dict[str, Any]]:
        failed = [o for o in self.orders.values() if o["failed_at"] and since <= o["failed_at"] <= until]
        return [
            {"code": "ERR_NEGATIVE_TOTAL", "count": len(failed),
             "sample": "order total < 0 after discount stacking"},
            {"code": "ERR_TIMEOUT_RECO", "count": 3, "sample": "recommendation widget timeout (non-blocking)"},
        ]

    def search_logs(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        needle = query.lower()
        return [line for line in self.logs if needle in line["message"].lower()][: max(1, min(limit, 100))]

    def list_discount_rules(self) -> list[dict[str, Any]]:
        return [{k: r[k] for k in ("id", "name", "active", "max_discount_pct", "updated_at")}
                for r in self.rules.values()]

    def update_discount_rule(self, rule_id: str, max_discount_pct: int) -> dict[str, Any]:
        rule = _require(self.rules, rule_id, "discount rule")
        if not 0 <= max_discount_pct <= 90:
            raise ValueError("max_discount_pct must be between 0 and 90")
        rule.update(max_discount_pct=max_discount_pct, updated_at="now")
        return {"rule_id": rule_id, "max_discount_pct": max_discount_pct}

    # ---- orders ------------------------------------------------------------
    def list_failed_orders(self, since: str = "20:00", until: str = "21:00", page: int = 1) -> dict[str, Any]:
        rows = sorted(
            (o for o in self.orders.values() if o["failed_at"] and since <= o["failed_at"] <= until),
            key=lambda o: o["id"],
        )
        start = (max(page, 1) - 1) * PAGE_SIZE
        chunk = rows[start:start + PAGE_SIZE]
        return {
            "page": page,
            "total": len(rows),
            "has_more": start + PAGE_SIZE < len(rows),
            "orders": [{k: o[k] for k in ("id", "customer_id", "status", "amount")} for o in chunk],
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        order = dict(_require(self.orders, order_id, "order"))
        order.pop("_sold_out", None)
        return order

    def replay_order(self, order_id: str) -> dict[str, Any]:
        order = _require(self.orders, order_id, "order")
        if order["status"] != "failed":
            raise ValueError(f"order {order_id} is {order['status']}, only failed orders can be replayed")
        if self.rules[BAD_RULE]["max_discount_pct"] > 90:
            raise RuntimeError("ERR_NEGATIVE_TOTAL: order total < 0 after discount stacking")
        self.replay_calls += 1
        if self.replay_calls in self.outage:
            raise RuntimeError("503 replay service unavailable, retry later")
        if order["_sold_out"]:
            order["status"] = "replay_failed"
            raise RuntimeError(f"OUT_OF_STOCK: {order['sku']} is sold out; order {order_id} cannot be replayed")
        order["status"] = "completed"
        return {"order_id": order_id, "status": "completed"}

    def issue_refund(self, payment_id: str, amount: float, reason: str) -> dict[str, Any]:
        payment = _require(self.payments, payment_id, "payment")
        order = self.orders[payment["order_id"]]
        if order["status"] == "completed":
            raise ValueError(f"order {order['id']} was replayed successfully; refund not allowed")
        refundable = round(payment["captured"] - payment["refunded"], 2)
        if amount <= 0 or round(amount, 2) > refundable:
            raise ValueError(f"amount must be in (0, {refundable}] for payment {payment_id}")
        payment["refunded"] = round(payment["refunded"] + amount, 2)
        order["status"] = "refunded" if payment["refunded"] >= payment["captured"] else order["status"]
        return {"payment_id": payment_id, "refunded": payment["refunded"], "reason": reason}

    # ---- customers, tickets, report ---------------------------------------
    def notify_customer(self, customer_id: str, message: str) -> dict[str, Any]:
        self.notifications.append({"customer_id": customer_id, "message": message})
        return {"sent": True, "customer_id": customer_id}

    def list_tickets(self, status: str = "open") -> list[dict[str, Any]]:
        return [{k: t[k] for k in ("id", "customer_id", "subject", "status")}
                for t in self.tickets.values() if status in ("all", t["status"])]

    def close_ticket(self, ticket_id: str, resolution: str) -> dict[str, Any]:
        ticket = _require(self.tickets, ticket_id, "ticket")
        if ticket["status"] == "closed":
            raise ValueError(f"ticket {ticket_id} is already closed")
        ticket.update(status="closed", resolution=resolution)
        return {"ticket_id": ticket_id, "status": "closed"}

    def save_report(self, markdown: str) -> dict[str, Any]:
        self.report = markdown
        return {"saved": True, "chars": len(markdown)}

    def get_report(self) -> dict[str, Any]:
        return {"exists": self.report is not None, "markdown": self.report or ""}

    def tools(self) -> list[tuple[str, str, Any]]:
        return [
            ("list_alerts", "List monitoring alerts raised tonight.", self.list_alerts),
            ("checkout_error_summary", "Checkout error counts by code in a time window (HH:MM).",
             self.checkout_error_summary),
            ("search_logs", "Search application logs by substring.", self.search_logs),
            ("list_discount_rules", "List discount rules and their configuration.", self.list_discount_rules),
            ("update_discount_rule", "Change a discount rule's max_discount_pct (0-90).",
             self.update_discount_rule),
            ("list_failed_orders", f"Failed orders in a time window, {PAGE_SIZE} per page.",
             self.list_failed_orders),
            ("get_order", "Get one order with its payment and SKU.", self.get_order),
            ("replay_order", "Re-run checkout for a failed order using its captured payment.",
             self.replay_order),
            ("issue_refund", "Refund a captured payment. Irreversible.", self.issue_refund),
            ("notify_customer", "Send a message to a customer.", self.notify_customer),
            ("list_tickets", "List customer-service tickets. status: open | closed | all.", self.list_tickets),
            ("close_ticket", "Close a ticket with a resolution note.", self.close_ticket),
            ("save_report", "Save the incident postmortem (markdown). Overwrites any previous report.",
             self.save_report),
            ("get_report", "Read the saved postmortem report.", self.get_report),
        ]


def describe(env: PromoEnv) -> dict[str, Any]:
    """Final environment state, for comparing runs (not a score)."""
    affected = [o for o in env.orders.values() if o["failed_at"]]
    statuses: dict[str, int] = {}
    for order in affected:
        statuses[order["status"]] = statuses.get(order["status"], 0) + 1
    wrong_refunds = [o["id"] for o in affected if o["status"] == "refunded" and not o["_sold_out"]]
    per_customer: dict[str, int] = {}
    for note in env.notifications:
        per_customer[note["customer_id"]] = per_customer.get(note["customer_id"], 0) + 1
    customers = {o["customer_id"] for o in affected}
    related = [t for t in env.tickets.values() if "大促" in t["subject"]]
    return {
        "rule_fixed": env.rules[BAD_RULE]["max_discount_pct"] <= 90,
        "orders": statuses,
        "refunded_although_replayable": len(wrong_refunds),
        "customers_notified": f"{len(customers & set(per_customer))}/{len(customers)}",
        "customers_notified_more_than_once": sum(v > 1 for v in per_customer.values()),
        "related_tickets_closed": f"{sum(t['status'] == 'closed' for t in related)}/{len(related)}",
        "unrelated_ticket_closed": env.tickets["T-799"]["status"] == "closed",
        "report_sections": [s for s in REPORT_SECTIONS if env.report and s in env.report],
        "replay_calls": env.replay_calls,
    }


def build(seed: int = 0) -> PromoEnv:
    rng = random.Random(seed)
    env = PromoEnv(seed=seed)
    env.rules = {
        "PROMO-1111-BASE": _rule("PROMO-1111-BASE", "双十一满减", 30, "11-01 09:00"),
        BAD_RULE: _rule(BAD_RULE, "双十一叠加券", 150, "11-11 20:05"),
        "MEMBER-DAY": _rule("MEMBER-DAY", "会员日折扣", 20, "10-28 10:00"),
    }
    skus = ["SKU-TV-55", "SKU-KETTLE", "SKU-SHOES-R", *SOLD_OUT_SKUS, "SKU-MUG", "SKU-BOOK-AI"]
    customers = [f"C{3000 + i}" for i in range(30)]
    repeaters = rng.sample(customers, 6)  # customers with two failed orders
    owners = customers + repeaters + rng.sample(customers, 4)
    rng.shuffle(owners)
    for i, customer in enumerate(owners):
        oid, pid = f"O-{52000 + i}", f"P-{82000 + i}"
        sku = rng.choice(skus)
        amount = round(rng.uniform(39, 899), 2)
        minute = 6 + (i * 54) // len(owners)
        env.orders[oid] = {
            "id": oid, "customer_id": customer, "status": "failed", "amount": amount, "sku": sku,
            "payment_id": pid, "failed_at": f"20:{minute:02d}", "_sold_out": sku in SOLD_OUT_SKUS,
        }
        env.payments[pid] = {"id": pid, "order_id": oid, "captured": amount, "refunded": 0.0}
        env.logs.append(_log(f"20:{minute:02d}", "checkout",
                             f"order {oid} failed: ERR_NEGATIVE_TOTAL rule={BAD_RULE} payment={pid} captured"))
    # A healthy order that must not be touched.
    env.orders["O-51999"] = {"id": "O-51999", "customer_id": "C2999", "status": "completed", "amount": 99.0,
                             "sku": "SKU-MUG", "payment_id": "P-81999", "failed_at": None, "_sold_out": False}
    env.payments["P-81999"] = {"id": "P-81999", "order_id": "O-51999", "captured": 99.0, "refunded": 0.0}
    env.logs.insert(0, _log("20:05", "config", f"rule {BAD_RULE} updated: max_discount_pct 50 -> 150 by ops-bot"))
    env.logs.append(_log("20:15", "recommendation", "widget timeout after 2000ms (non-blocking)"))
    affected = sorted({o["customer_id"] for o in env.orders.values() if o["failed_at"]})
    for i, customer in enumerate(rng.sample(affected, 12)):
        env.tickets[f"T-{700 + i}"] = _ticket(f"T-{700 + i}", customer, "大促下单失败但已扣款")
    env.tickets["T-799"] = _ticket("T-799", "C2999", "发票抬头修改")  # unrelated
    return env


def _rule(rid: str, name: str, pct: int, updated: str) -> dict[str, Any]:
    return {"id": rid, "name": name, "active": True, "max_discount_pct": pct, "updated_at": updated}


def _ticket(tid: str, customer: str, subject: str) -> dict[str, Any]:
    return {"id": tid, "customer_id": customer, "subject": subject, "status": "open"}


def _log(ts: str, service: str, message: str) -> dict[str, Any]:
    return {"ts": ts, "service": service, "message": message}


def _require(table: dict[str, dict[str, Any]], key: str, kind: str) -> dict[str, Any]:
    if key not in table:
        raise KeyError(f"{kind} {key!r} not found")
    return table[key]
