"""Lab4 scenario: a queue of 10 customer-service tickets.

Each ticket is handled by a fresh agent context (the Lab4 driver does that), so
anything learned on one ticket survives only through memory. Three long SOPs
are available via ``read_policy``; following them is the job of skills.

Cross-ticket information:

* T-802: investigating reveals the PG-B gateway re-captures on retry and the
  retry capture is not linked to the order (known issue INC-311, only visible in
  the payment logs); T-806 and T-809 are the same problem with vaguer wording;
* T-801: customer C5001 says to contact them by SMS only; T-807 is C5001 again;
* T-803: the QA feedback sets a rule for all later replies (ticket id first).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any

from labkit.policies import POLICIES

ID = "lab4-ticket-queue"
INSTRUCTION = "依次处理客服队列中的全部工单，直到队列为空。每张工单按公司政策处理，完成后提交处理结果。"

CHANNELS = ("sms", "email", "app")


def _ticket(tid, cid, oid, subject, body):
    return {"id": tid, "customer_id": cid, "order_id": oid, "subject": subject, "body": body, "status": "open"}


TICKETS = [
    _ticket("T-801", "C5001", "O-7001", "包裹延误", "包裹比承诺的晚了好几天才到。另外说明一下：以后联系我只发短信，"
            "不要用 App 推送，也不要发邮件。"),
    _ticket("T-802", "C5002", "O-7002", "重复扣款", "我的银行卡显示这个订单被扣了两次 298 元，"
            "银行流水上的交易参考号是 P-9021 和 P-9022，请核实。"),
    _ticket("T-803", "C5003", "O-7003", "商品破损", "收到的杯子碎了，照片已上传，申请退款。"),
    _ticket("T-804", "C5004", "O-7004", "物流问题", "第三次催单了还没到，再不给说法我就去消协投诉，也已经咨询了律师。"),
    _ticket("T-805", "C5005", "O-7005", "包裹延误", "比预计晚了两天，要求补偿。"),
    _ticket("T-806", "C5006", "O-7006", "扣款疑问", "账单里有两笔一模一样的扣款，是不是出问题了？"),
    _ticket("T-807", "C5001", "O-7007", "少发配件", "耳机少了充电线，申请退这部分的钱。"),
    _ticket("T-808", "C5008", "O-7008", "包裹延误", "下单快两周了才收到，太慢了。"),
    _ticket("T-809", "C5009", "O-7009", "扣款疑问", "好像被多扣了一笔钱，麻烦看一下。"),
    _ticket("T-810", "C5010", "O-7010", "退货退款", "退货已经寄回并签收，请退款。"),
]

# What following the SOPs means for each ticket; used only by describe().
EXPECT: dict[str, dict[str, Any]] = {
    "T-801": {"coupon": 30},
    "T-802": {"refund": ("P-9022", 298.0), "approval": True},
    "T-803": {"refund": ("P-9031", 120.0)},
    "T-804": {"escalate": True},
    "T-805": {"coupon": 0},
    "T-806": {"refund": ("P-9062", 650.0), "approval": True},
    "T-807": {"refund": ("P-9071", 89.0), "channel": "sms"},
    "T-808": {"coupon": 50},
    "T-809": {"refund": ("P-9092", 156.0)},
    "T-810": {"escalate": True},
}
FEEDBACK_RULE_FROM = "T-803"
FEEDBACK = {
    "T-803": "质检意见：回复客户时，第一行必须写明工单号，格式为【T-xxx】。这条规则适用于之后的所有工单。",
}


@dataclass
class TicketDesk:
    seed: int = 0
    tickets: list[dict[str, Any]] = field(default_factory=list)
    customers: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    shipments: dict[str, dict[str, Any]] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    coupons: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    resolutions: dict[str, str] = field(default_factory=dict)
    policy_reads: list[tuple[str, str]] = field(default_factory=list)  # (ticket, policy)
    steps: dict[str, int] = field(default_factory=dict)
    cursor: int = 0

    @property
    def current(self) -> dict[str, Any] | None:
        return self.tickets[self.cursor] if self.cursor < len(self.tickets) else None

    def _tid(self) -> str:
        return self.current["id"] if self.current else "-"

    # ---- queue ---------------------------------------------------------------------
    def next_ticket(self) -> dict[str, Any]:
        ticket = self.current
        if ticket is None:
            return {"done": True, "message": "队列已空"}
        return {k: ticket[k] for k in ("id", "customer_id", "order_id", "subject", "body")}

    def submit_resolution(self, ticket_id: str, summary: str) -> dict[str, Any]:
        ticket = self.current
        if ticket is None or ticket["id"] != ticket_id:
            raise ValueError(f"当前待处理的工单是 {self._tid()}，不能提交 {ticket_id}")
        ticket["status"] = "resolved"
        self.resolutions[ticket_id] = summary
        self.cursor += 1
        return {"ticket_id": ticket_id, "status": "resolved", "next": self._tid()}

    def skip_ticket(self, ticket_id: str, reason: str) -> dict[str, Any]:
        ticket = self.current
        if ticket is None or ticket["id"] != ticket_id:
            raise ValueError(f"当前待处理的工单是 {self._tid()}")
        ticket["status"] = "skipped"
        self.resolutions[ticket_id] = f"[skipped] {reason}"
        self.cursor += 1
        return {"ticket_id": ticket_id, "status": "skipped"}

    def get_feedback(self, ticket_id: str) -> dict[str, Any]:
        ticket = next((t for t in self.tickets if t["id"] == ticket_id), None)
        if ticket is None or ticket["status"] == "open":
            raise ValueError("只能查看已提交工单的质检意见")
        return {"ticket_id": ticket_id, "feedback": FEEDBACK.get(ticket_id, "质检意见：处理合格。")}

    # ---- lookup --------------------------------------------------------------------
    def list_policies(self) -> list[dict[str, str]]:
        return [{"name": name, "summary": summary} for name, (summary, _) in POLICIES.items()]

    def read_policy(self, policy: str) -> str:
        if policy not in POLICIES:
            raise KeyError(f"policy {policy!r} not found; available: {sorted(POLICIES)}")
        self.policy_reads.append((self._tid(), policy))
        return POLICIES[policy][1]

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        return dict(_require(self.customers, customer_id, "customer"))

    def get_order(self, order_id: str) -> dict[str, Any]:
        return dict(_require(self.orders, order_id, "order"))

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return dict(_require(self.payments, payment_id, "payment"))

    def get_payments(self, order_id: str) -> list[dict[str, Any]]:
        _require(self.orders, order_id, "order")
        return [dict(p) for p in self.payments.values() if p["order_id"] == order_id]

    def get_shipment(self, order_id: str) -> dict[str, Any]:
        return dict(_require(self.shipments, order_id, "shipment"))

    def search_logs(self, query: str) -> list[str]:
        """Every whitespace-separated term must appear in the line (case-insensitive)."""
        terms = query.lower().split()
        return [line for line in self.logs if all(t in line.lower() for t in terms)][:30]

    # ---- actions -------------------------------------------------------------------
    def request_approval(self, payment_id: str, amount: float, reason: str) -> dict[str, Any]:
        payment = _require(self.payments, payment_id, "payment")
        expected = [e.get("refund") for e in EXPECT.values() if e.get("approval")]
        ok = (payment_id, round(amount, 2)) in [(p, a) for p, a in expected if p] and amount <= payment["amount"]
        approval_id = f"AP-{len(self.approvals) + 1:03d}"
        self.approvals[approval_id] = {"approval_id": approval_id, "payment_id": payment_id, "amount": amount,
                                       "status": "approved" if ok else "rejected", "ticket": self._tid()}
        return dict(self.approvals[approval_id])

    def issue_refund(self, payment_id: str, amount: float, reason: str) -> dict[str, Any]:
        payment = _require(self.payments, payment_id, "payment")
        refundable = round(payment["amount"] - payment["refunded"], 2)
        if payment["status"] != "captured" or amount <= 0 or round(amount, 2) > refundable:
            raise ValueError(f"amount must be in (0, {refundable}] and payment must be captured")
        payment["refunded"] = round(payment["refunded"] + amount, 2)
        approved = any(a["payment_id"] == payment_id and a["status"] == "approved" for a in self.approvals.values())
        self.refunds.append({"ticket": self._tid(), "payment_id": payment_id, "amount": round(amount, 2),
                             "reason": reason, "approved": approved})
        return {"payment_id": payment_id, "refunded": payment["refunded"]}

    def issue_coupon(self, customer_id: str, amount: float, reason: str) -> dict[str, Any]:
        _require(self.customers, customer_id, "customer")
        self.coupons.append({"ticket": self._tid(), "customer_id": customer_id, "amount": amount, "reason": reason})
        return {"coupon_id": f"CP-{len(self.coupons):04d}", "amount": amount, "valid_days": 30}

    def send_message(self, customer_id: str, channel: str, text: str) -> dict[str, Any]:
        _require(self.customers, customer_id, "customer")
        if channel not in CHANNELS:
            raise ValueError(f"channel must be one of {CHANNELS}")
        self.messages.append({"ticket": self._tid(), "customer_id": customer_id, "channel": channel, "text": text})
        return {"sent": True, "channel": channel}

    def escalate_to_human(self, ticket_id: str, reason: str) -> dict[str, Any]:
        self.escalations.append({"ticket": ticket_id, "reason": reason})
        return {"ticket_id": ticket_id, "escalated": True}

    def tools(self) -> list[tuple[str, str, Any]]:
        spec = [
            ("next_ticket", "取当前待处理的工单（提交后才会前进到下一张）。", self.next_ticket),
            ("submit_resolution", "提交当前工单的处理结果，队列前进到下一张。", self.submit_resolution),
            ("skip_ticket", "无法处理时放弃当前工单，转入人工队列。", self.skip_ticket),
            ("get_feedback", "查看已提交工单的质检意见。", self.get_feedback),
            ("list_policies", "列出公司政策文档。", self.list_policies),
            ("read_policy", "读取一份政策文档全文。", self.read_policy),
            ("get_customer", "查询客户资料（含 tier、默认联系渠道）。", self.get_customer),
            ("get_order", "查询订单。", self.get_order),
            ("get_payments", "查询订单关联的支付记录。", self.get_payments),
            ("get_payment", "按支付单号查询一笔支付。", self.get_payment),
            ("get_shipment", "查询物流：承诺送达日期、实际送达日期、备注。", self.get_shipment),
            ("search_logs", "按关键词搜索系统日志；多个关键词用空格分隔，需同时出现。", self.search_logs),
            ("request_approval", "为退款申请审批。", self.request_approval),
            ("issue_refund", "对一笔支付退款。", self.issue_refund),
            ("issue_coupon", "给客户发优惠券。", self.issue_coupon),
            ("send_message", "给客户发消息。channel: sms | email | app。", self.send_message),
            ("escalate_to_human", "把工单转给人工坐席。", self.escalate_to_human),
        ]
        return [(name, description, self._counted(fn)) for name, description, fn in spec]

    def _counted(self, fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tid = self._tid()
            self.steps[tid] = self.steps.get(tid, 0) + 1
            return fn(*args, **kwargs)

        return wrapper


ITEMS = {"O-7007": [{"name": "降噪耳机", "price": 310.0}, {"name": "充电线", "price": 89.0}]}


def build(seed: int = 0) -> TicketDesk:
    desk = TicketDesk(seed=seed, tickets=[dict(t) for t in TICKETS])
    tiers = {"C5004": "vip", "C5010": "vip"}
    for t in TICKETS:
        cid = t["customer_id"]
        desk.customers[cid] = {"customer_id": cid, "tier": tiers.get(cid, "normal"), "default_channel": "app"}
    orders = {
        "O-7001": (219.0, [("P-9011", "PG-A")], ("11-01", "11-06", "")),
        "O-7002": (298.0, [("P-9021", "PG-B"), ("P-9022", "PG-B")], ("11-02", "11-02", "")),
        "O-7003": (120.0, [("P-9031", "PG-A")], ("11-02", "11-02", "")),
        "O-7004": (460.0, [("P-9041", "PG-A")], ("10-30", None, "在途，转运中心滞留")),
        "O-7005": (88.0, [("P-9051", "PG-A")], ("11-01", "11-03", "客户 10-30 发货后修改了收货地址")),
        "O-7006": (650.0, [("P-9061", "PG-B"), ("P-9062", "PG-B")], ("11-03", "11-03", "")),
        "O-7007": (399.0, [("P-9071", "PG-A")], ("11-03", "11-03", "")),
        "O-7008": (156.0, [("P-9081", "PG-A")], ("10-24", "11-02", "")),
        "O-7009": (156.0, [("P-9091", "PG-B"), ("P-9092", "PG-B")], ("11-04", "11-04", "")),
        "O-7010": (760.0, [("P-9101", "PG-A")], ("10-28", "10-28", "退货已签收")),
    }
    for oid, (total, pays, (promised, delivered, notes)) in orders.items():
        cid = next(t["customer_id"] for t in TICKETS if t["order_id"] == oid)
        desk.orders[oid] = {"id": oid, "customer_id": cid, "total": total, "status": "paid",
                            "items": ITEMS.get(oid, [{"name": "商品", "price": total}])}
        for n, (pid, gateway) in enumerate(pays):
            # A PG-B retry capture is not linked to the order (INC-311): it shows up in the
            # gateway logs only, so get_payments lists a single payment for that order.
            desk.payments[pid] = {"payment_id": pid, "order_id": oid if n == 0 else None, "amount": total,
                                  "gateway": gateway, "status": "captured", "refunded": 0.0,
                                  "captured_at": f"2026-{promised}T20:01:{5 + 36 * n:02d}"}
            if n:
                desk.payments[pid]["note"] = "该笔扣款未关联订单（PG-B 重试扣款，详见日志中的 INC-311）"
        desk.shipments[oid] = {"order_id": oid, "promised": f"2026-{promised}",
                               "delivered": f"2026-{delivered}" if delivered else None, "notes": notes}
    desk.logs = [
        "2026-11-02T20:01:05 payment PG-B capture ok P-9021 order=O-7002",
        "2026-11-02T20:01:38 payment PG-B gateway timeout, client retry order=O-7002",
        "2026-11-02T20:01:41 payment PG-B capture ok P-9022 order=O-7002 (retry, not linked to order)",
        "2026-11-02T21:00:00 ops INC-311 opened: when the client retries after a PG-B gateway timeout, PG-B "
        "captures again and the retry capture is NOT linked to the order, so it does not appear in the order's "
        "payment list. Find it in the payment logs by order id, then refund that capture. Fix scheduled 11-20.",
        "2026-11-03T09:12:00 recommendation latency high (unrelated)",
        "2026-11-03T20:01:05 payment PG-B capture ok P-9061 order=O-7006",
        "2026-11-03T20:01:41 payment PG-B capture ok P-9062 order=O-7006 (retry, not linked to order)",
        "2026-11-04T20:01:05 payment PG-B capture ok P-9091 order=O-7009",
        "2026-11-04T20:01:41 payment PG-B capture ok P-9092 order=O-7009 (retry, not linked to order)",
    ] + [f"2026-11-0{d}T1{h}:00:00 checkout ok order=O-6{d}{h}0{n}" for d in range(1, 5) for h in range(10)
         for n in range(3)]
    return desk


def describe(desk: TicketDesk) -> dict[str, Any]:
    """Per-ticket final state against the SOPs (a description, not a score)."""
    rows = []
    rule_active = False
    for ticket in desk.tickets:
        tid = ticket["id"]
        exp = EXPECT[tid]
        refunds = [r for r in desk.refunds if r["ticket"] == tid]
        coupons = [c["amount"] for c in desk.coupons if c["ticket"] == tid]
        messages = [m for m in desk.messages if m["ticket"] == tid]
        escalated = any(e["ticket"] == tid for e in desk.escalations)
        row: dict[str, Any] = {"ticket": tid, "status": ticket["status"], "steps": desk.steps.get(tid, 0),
                               "policies_read": sorted({p for t, p in desk.policy_reads if t == tid})}
        if "refund" in exp:
            pid, amount = exp["refund"]
            row["refund_ok"] = [(r["payment_id"], r["amount"]) for r in refunds] == [(pid, amount)]
            if exp.get("approval"):
                row["approved_before_refund"] = bool(refunds) and all(r["approved"] for r in refunds)
        elif refunds:
            row["unexpected_refunds"] = [(r["payment_id"], r["amount"]) for r in refunds]
        if exp.get("escalate"):
            row["escalated_without_compensation"] = escalated and not refunds and not coupons
        elif escalated:
            row["unnecessary_escalation"] = True
        if "coupon" in exp:
            row["coupon_ok"] = coupons == ([exp["coupon"]] if exp["coupon"] else [])
        if "channel" in exp:
            row["channel_ok"] = bool(messages) and all(m["channel"] == exp["channel"] for m in messages)
        if rule_active:
            row["ticket_id_first_line"] = bool(messages) and all(
                m["text"].strip().splitlines()[0].startswith(f"【{tid}】") for m in messages)
        if tid == FEEDBACK_RULE_FROM:
            rule_active = True
        rows.append(row)
    return {
        "tickets": rows,
        "resolved": sum(t["status"] == "resolved" for t in desk.tickets),
        "steps_first_half": sum(desk.steps.get(t["id"], 0) for t in desk.tickets[:5]),
        "steps_second_half": sum(desk.steps.get(t["id"], 0) for t in desk.tickets[5:]),
        "policy_reads": len(desk.policy_reads),
    }


def _require(table: dict[str, dict[str, Any]], key: str, kind: str) -> dict[str, Any]:
    if key not in table:
        raise KeyError(f"{kind} {key!r} not found")
    return table[key]
