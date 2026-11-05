"""Lab4 driver: one fresh agent per ticket.

The queue is worked ticket by ticket. Each ticket gets a newly built agent with
an empty conversation, exactly like a support desk where every case is a new
session. The only thing that carries over is the run's copy of ``lab4/memory``.
"""

from __future__ import annotations

from typing import Any

from core.lab import Lab, RunContext, build_agent

MAX_TICKETS = 20
TICKET_PROMPT = """处理工单 {id}（{subject}）
客户：{customer_id}　订单：{order_id}
客户留言：{body}

按公司政策处理：需要时查询订单、支付、物流、客户资料；用 send_message 回复客户；完成后调用 submit_resolution 提交本工单。"""


class QueueResult:
    def __init__(self, handled: list[dict[str, Any]]) -> None:
        self.stop_reason = "end_turn"
        lines = [f"{h['ticket']}: {h['outcome']}" for h in handled]
        self.message = {"role": "assistant", "content": [{"text": "队列处理完毕：\n" + "\n".join(lines)}]}


def driver(lab: Lab, ctx: RunContext) -> QueueResult:
    from strands.types.agent import Limits
    from strands.types.exceptions import MaxTokensReachedException

    limits = Limits(turns=ctx.config.get("limits", "max_turns"))
    handled = []
    for index in range(1, MAX_TICKETS + 1):
        if ctx.budget.exhausted():
            ctx.trace.feature("queue_stopped", reason="token budget exhausted")
            break
        ticket = ctx.call_env("next_ticket")
        if ticket.get("done"):
            break
        ctx.summary["current_ticket"] = ticket
        ctx.trace.feature("ticket_start", ticket=ticket["id"], index=index)
        agent, _ = build_agent(lab, ctx, label=ticket["id"])  # fresh context
        try:
            agent(TICKET_PROMPT.format(**ticket), limits=limits)
        except MaxTokensReachedException:
            ctx.trace.feature("ticket_max_tokens", ticket=ticket["id"])
        after = ctx.call_env("next_ticket")
        if after.get("id") == ticket["id"]:  # the agent ended without submitting
            ctx.call_env("skip_ticket", ticket_id=ticket["id"], reason="agent ended without submitting")
            outcome = "skipped"
        else:
            outcome = "resolved"
        ctx.trace.feature("ticket_end", ticket=ticket["id"], outcome=outcome, messages=len(agent.messages))
        handled.append({"ticket": ticket["id"], "outcome": outcome})
    ctx.summary.pop("current_ticket", None)
    return QueueResult(handled)
