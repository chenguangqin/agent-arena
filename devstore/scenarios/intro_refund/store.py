"""In-memory online store state and the actions agents can take on it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Store:
    customers: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    # Side effects performed by the agent, inspected by scenario checks.
    refunds: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)

    # ---- read operations -------------------------------------------------
    def list_tickets(self, status: str | None = "open") -> list[dict[str, Any]]:
        return [
            {k: t[k] for k in ("id", "customer_id", "subject", "status")}
            for t in self.tickets.values()
            if status in (None, "", "all") or t["status"] == status
        ]

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return dict(self._require(self.tickets, ticket_id, "ticket"))

    def search_orders(self, customer_id: str, status: str | None = None) -> list[dict[str, Any]]:
        return [
            {k: o[k] for k in ("id", "status", "amount", "created_at")}
            for o in self.orders.values()
            if o["customer_id"] == customer_id and (not status or o["status"] == status)
        ]

    def get_order(self, order_id: str) -> dict[str, Any]:
        return dict(self._require(self.orders, order_id, "order"))

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return dict(self._require(self.payments, payment_id, "payment"))

    def search_logs(
        self, query: str, service: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        needle = query.lower()
        hits = [
            line
            for line in self.logs
            if needle in line["message"].lower() and (not service or line["service"] == service)
        ]
        return hits[: max(1, min(limit, 200))]

    # ---- write operations ------------------------------------------------
    def issue_refund(self, payment_id: str, amount: float, reason: str) -> dict[str, Any]:
        payment = self._require(self.payments, payment_id, "payment")
        refundable = round(payment["captured"] - payment["refunded"], 2)
        if amount <= 0 or round(amount, 2) > refundable:
            raise ValueError(f"amount must be in (0, {refundable}] for payment {payment_id}")
        payment["refunded"] = round(payment["refunded"] + amount, 2)
        record = {"payment_id": payment_id, "amount": round(amount, 2), "reason": reason}
        self.refunds.append(record)
        return {"refund_id": f"R-{len(self.refunds):04d}", **record}

    def send_notification(self, customer_id: str, message: str) -> dict[str, Any]:
        self._require(self.customers, customer_id, "customer")
        self.notifications.append({"customer_id": customer_id, "message": message})
        return {"sent": True, "customer_id": customer_id}

    def close_ticket(self, ticket_id: str, resolution: str) -> dict[str, Any]:
        ticket = self._require(self.tickets, ticket_id, "ticket")
        if ticket["status"] == "closed":
            raise ValueError(f"ticket {ticket_id} is already closed")
        ticket.update(status="closed", resolution=resolution)
        return {"ticket_id": ticket_id, "status": "closed"}

    def tools(self) -> list[tuple[str, str, Any]]:
        """Tools served over MCP: (name, description, function)."""
        return [
            ("list_tickets", "List customer-service tickets. status: open | closed | all.", self.list_tickets),
            ("get_ticket", "Get one ticket with its full text.", self.get_ticket),
            ("search_orders", "List a customer's orders, optionally filtered by status.", self.search_orders),
            ("get_order", "Get one order, including its payment_id if a payment exists.", self.get_order),
            ("get_payment", "Get a payment: captured amount and amount already refunded.", self.get_payment),
            ("search_logs", "Search application logs by substring. service: checkout | payment.", self.search_logs),
            ("issue_refund", "Refund part or all of a captured payment. Irreversible.", self.issue_refund),
            ("send_notification", "Send a message to a customer.", self.send_notification),
            ("close_ticket", "Close a ticket with a resolution note.", self.close_ticket),
        ]

    @staticmethod
    def _require(table: dict[str, dict[str, Any]], key: str, kind: str) -> dict[str, Any]:
        if key not in table:
            raise KeyError(f"{kind} {key!r} not found")
        return table[key]
