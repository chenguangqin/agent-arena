"""Intro scenario for the M1 demo: one ticket, refund + notify + close.

Distractors: an uncharged failed order, an already refunded order, and another
customer's failed order that must not be touched.
"""

from __future__ import annotations

from typing import Any

from devstore.scenarios.intro_refund.store import Store

ID = "intro-refund"
INSTRUCTION = (
    "处理客服工单 T-501：客户反映 checkout 失败但被扣了款。"
    "核实情况，对确实被扣款但订单失败的部分全额退款，通知客户处理结果，并关闭工单。"
)


def build(seed: int = 0) -> Store:
    return Store(
        customers={
            "C1024": {"id": "C1024", "name": "林女士"},
            "C2048": {"id": "C2048", "name": "周先生"},
        },
        orders={
            "O-9001": _order("O-9001", "C1024", "paid", 129.00, "P-7001", "2026-11-01T10:02:00"),
            "O-9002": _order("O-9002", "C1024", "failed", 349.00, "P-7002", "2026-11-03T20:15:00"),
            "O-9003": _order("O-9003", "C1024", "failed", 59.00, None, "2026-11-03T20:21:00"),
            "O-9004": _order("O-9004", "C1024", "cancelled", 88.00, "P-7004", "2026-10-28T09:40:00"),
            "O-9100": _order("O-9100", "C2048", "failed", 520.00, "P-7100", "2026-11-03T20:18:00"),
        },
        payments={
            "P-7001": _payment("P-7001", "O-9001", 129.00, 0.0),
            "P-7002": _payment("P-7002", "O-9002", 349.00, 0.0),
            "P-7004": _payment("P-7004", "O-9004", 88.00, 88.00),
            "P-7100": _payment("P-7100", "O-9100", 520.00, 0.0),
        },
        tickets={
            "T-501": {
                "id": "T-501",
                "customer_id": "C1024",
                "subject": "checkout 失败但被扣款",
                "body": "11月3日晚上下单两次都提示支付失败，但银行卡被扣了一笔钱。",
                "status": "open",
            },
            "T-502": {
                "id": "T-502",
                "customer_id": "C2048",
                "subject": "订单状态查询",
                "body": "想确认一下我的订单什么时候发货。",
                "status": "open",
            },
        },
        logs=[
            _log("2026-11-03T20:15:02", "payment", "capture succeeded payment=P-7002 order=O-9002 amount=349.00"),
            _log("2026-11-03T20:15:04", "checkout", "inventory reservation timeout order=O-9002"),
            _log("2026-11-03T20:15:04", "checkout", "order O-9002 marked failed after payment capture"),
            _log("2026-11-03T20:21:10", "payment", "authorization declined order=O-9003 reason=card_limit"),
            _log("2026-11-03T20:21:10", "checkout", "order O-9003 marked failed, no capture"),
            _log("2026-11-03T20:18:30", "checkout", "order O-9100 marked failed after payment capture"),
        ],
    )


def _order(oid, cid, status, amount, payment_id, created_at) -> dict[str, Any]:
    return {
        "id": oid,
        "customer_id": cid,
        "status": status,
        "amount": amount,
        "payment_id": payment_id,
        "created_at": created_at,
    }


def _payment(pid, order_id, captured, refunded) -> dict[str, Any]:
    return {"id": pid, "order_id": order_id, "captured": captured, "refunded": refunded}


def _log(ts, service, message) -> dict[str, Any]:
    return {"ts": ts, "service": service, "message": message}
