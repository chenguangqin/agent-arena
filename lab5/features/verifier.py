"""L5.6 · Verifier sub-agent.

A tool the main agent calls before finishing. The verifier runs in its own
context with read-only tools and its own instructions, inspects the
environment, and reports discrepancies. It does not see the main agent's
reasoning, so it is not persuaded by it.
"""

from __future__ import annotations

from strands import tool

from core.lab import AgentSpec, RunContext, helper_agent
from lab5.features.common import sub_model_id

READ_ONLY = ("list_discount_rules", "checkout_error_summary", "list_failed_orders", "get_order",
             "list_tickets", "get_report")
PROMPT = (
    "你是核查员，只有只读工具。根据给出的任务要求，逐项检查环境中的实际状态是否满足，"
    "列出所有不一致之处（编号、数量、缺失项）。不要修改任何东西，不要推测，只报告查到的事实。"
)
GUIDE = """
## 核查
- 调用 finish 之前，先调用 verify_work 让独立核查员检查结果，并处理它发现的问题。
"""


def install(spec: AgentSpec, ctx: RunContext) -> None:
    @tool
    def verify_work(focus: str = "全部要求") -> str:
        """Ask an independent read-only verifier to check the environment against the task.
        focus: which requirements to check (default: all)."""
        verifier = helper_agent(
            ctx, label="verifier", system_prompt=PROMPT, model_id=sub_model_id(ctx),
            tools=[ctx.env_tool(name) for name in READ_ONLY],
        )
        report = str(verifier(f"任务要求：\n{ctx.instruction}\n\n本次核查重点：{focus}")).strip()
        ctx.trace.feature("verifier", focus=focus, findings=report[:600])
        return report

    spec.tools.append(verify_work)
    spec.system_prompt += GUIDE
