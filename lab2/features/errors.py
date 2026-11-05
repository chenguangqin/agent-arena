"""L2.4 · Actionable errors and retries.

The raw API answers with bare codes such as ``ERR_4012``. An
``AfterToolCallEvent`` hook rewrites each error into cause + next step, and
retries transient ``ERR_5031`` automatically (``event.retry``) a bounded
number of times before showing it to the model.
"""

from __future__ import annotations

import re
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from core.lab import AgentSpec, RunContext
from core.trace import result_text
from lab2.features.common import append_text, canonical, parse_result

EXPLAIN = {
    "ERR_5031": "支付网关暂时超时。这是临时故障，稍后重试同一请求即可。",
    "ERR_4012": "退款被拒：金额超过 500 元，需要审批。先调用 request_refund_approval（approvals_request）"
                "为这笔支付申请审批，批准后再退款。",
    "ERR_4090": "退款金额无效：金额必须大于 0、不超过该支付的可退金额（amount - refunded_amount），"
                "且支付状态必须是 captured。先查询这笔支付再决定金额。",
    "ERR_4041": "对象不存在：检查编号是否正确（订单 O-xxxxx，支付 PAY-xxxxx，客户 Cxxxx）。",
    "ERR_4220": "请求无法处理：通知模板不存在或缺少必需参数。先列出通知模板，按模板要求传 params。",
}
MAX_RETRIES = 3


class ErrorFeedback(HookProvider):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.retries: dict[str, int] = {}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        if event.result.get("status") != "error":
            self._empty_hint(event)
            return
        text = result_text(event.result)
        match = re.search(r"ERR_\d{4}", text)
        if not match:
            return
        code = match.group(0)
        tool = canonical(event.tool_use.get("name", ""))
        use_id = event.tool_use.get("toolUseId", "")
        if code == "ERR_5031" and self.retries.get(use_id, 0) < MAX_RETRIES:
            self.retries[use_id] = self.retries.get(use_id, 0) + 1
            self.ctx.trace.feature("auto_retry", tool=tool, code=code, attempt=self.retries[use_id])
            event.retry = True
            return
        self.ctx.trace.feature("error_explained", tool=tool, code=code)
        event.result = append_text(event.result, f"[harness] {code}：{EXPLAIN.get(code, '未知错误码')}")


    def _empty_hint(self, event: AfterToolCallEvent) -> None:
        """An empty list is often a wrong parameter (date, id format), not 'nothing to do'."""
        payload = parse_result(event.result)
        empty = (isinstance(payload, list) and not payload) or (
            isinstance(payload, dict) and any(payload.get(k) in ([], 0) for k in ("data", "duplicates", "count"))
            and not payload.get("data") and not payload.get("duplicates"))
        if not empty:
            return
        tool = canonical(event.tool_use.get("name", ""))
        self.ctx.trace.feature("empty_result_hint", tool=tool, input=event.tool_use.get("input"))
        event.result = append_text(
            event.result,
            "[harness] 查询结果为空。在得出“没有数据”的结论前，先核对参数：日期格式与年份是否和任务一致、"
            "编号是否正确。必要时换一个查询方式确认。",
        )


def install(spec: AgentSpec, ctx: RunContext) -> None:
    spec.hooks.append(ErrorFeedback(ctx))
