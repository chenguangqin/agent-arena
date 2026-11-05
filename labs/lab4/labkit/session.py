"""One ticket = one session: a new Agent with an empty context.

labkit drives the queue. For each ticket it builds a fresh agent through your
``build_agent(env, mode, session)``, runs it, records the trajectory, and then
calls your ``on_session_end(env, mode, session)``. The only thing that carries
over from one ticket to the next is what you write under ``session.memory_dir``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TICKET_PROMPT = """处理工单 {id}（{subject}）
客户：{customer_id}　订单：{order_id}
客户留言：{body}

按公司政策处理：需要时查询订单、支付、物流、客户资料；用 send_message 回复客户；完成后调用 submit_resolution 提交本工单。"""


@dataclass
class Trajectory:
    """What happened in one session; filled in by labkit after the agent finishes."""

    ticket_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)  # the agent's full conversation
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {"name", "input", "status", "result"}
    reply: str = ""  # the agent's final text
    submitted: bool = False
    feedback: str = ""  # QA feedback from the support desk, after submission

    def to_dict(self) -> dict[str, Any]:
        return {"ticket_id": self.ticket_id, "tool_calls": self.tool_calls, "reply": self.reply,
                "submitted": self.submitted, "feedback": self.feedback}


@dataclass
class Session:
    index: int  # 1-based position in the queue
    ticket: dict[str, Any] | None  # None for a free chat session
    memory_dir: Path  # persistent across sessions — the only carry-over
    skills_dir: Path
    trajectory: Trajectory | None = None

    @property
    def prompt(self) -> str:
        return TICKET_PROMPT.format(**self.ticket) if self.ticket else ""


def collect_trajectory(agent: Any, ticket_id: str) -> Trajectory:
    """Pair every toolUse in the conversation with its toolResult."""
    traj = Trajectory(ticket_id=ticket_id, messages=json.loads(json.dumps(agent.messages, default=str)))
    pending: dict[str, dict[str, Any]] = {}
    for message in traj.messages:
        for block in message.get("content", []):
            if "toolUse" in block:
                use = block["toolUse"]
                call = {"name": use.get("name"), "input": use.get("input"), "status": None, "result": ""}
                pending[use.get("toolUseId")] = call
                traj.tool_calls.append(call)
            elif "toolResult" in block:
                result = block["toolResult"]
                call = pending.get(result.get("toolUseId"))
                if call is not None:
                    call["status"] = result.get("status")
                    call["result"] = "\n".join(c.get("text", "") for c in result.get("content", []))[:3000]
            elif "text" in block and message.get("role") == "assistant":
                traj.reply = block["text"]
    return traj
