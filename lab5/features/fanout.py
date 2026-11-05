"""L5.7 · Fan-out to worker sub-agents.

``process_orders`` splits order ids into batches and hands each batch to a
worker agent running in parallel. Workers have only the tools needed for one
order (get / replay / refund), handle retries and the replay-or-refund branch
themselves, and return one line per order. The main agent receives a compact
table, so its context does not grow with the number of orders.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from strands import tool

from core.lab import AgentSpec, RunContext, helper_agent
from lab5.features.common import sub_model_id

WORKER_TOOLS = ("get_order", "replay_order", "issue_refund")
WORKER_PROMPT = (
    "你负责处理一批失败订单。对每个订单：先 replay_order；如果返回 503，先处理其他订单，最后再重试，"
    "最多重试 3 次；如果返回库存售罄等无法重放的错误，用 get_order 查到 payment_id 和金额，全额 issue_refund。"
    "同一订单不能既重放成功又退款。全部处理完后，每个订单输出一行 JSON："
    '{"order_id":..., "customer_id":..., "outcome":"completed|refunded|failed", "detail":...}，不要输出其他内容。'
)
GUIDE = """
## 批量处理订单
- 需要重放或退款的订单很多时，用 process_orders 一次交给并行的处理员，你只需要汇总结果。
"""


def install(spec: AgentSpec, ctx: RunContext) -> None:
    batch_size = ctx.config.get("orchestration", "batch_size")
    max_workers = ctx.config.get("orchestration", "max_workers")

    @tool
    def process_orders(order_ids: list[str]) -> str:
        """Replay each failed order, refunding instead when replay is impossible. Runs batches in
        parallel worker agents and returns one JSON line per order."""
        ids = list(dict.fromkeys(order_ids))
        batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
        ctx.trace.feature("fanout", orders=len(ids), batches=len(batches), workers=min(max_workers, len(batches)))

        def work(index: int, batch: list[str]) -> list[dict]:
            worker = helper_agent(
                ctx, label=f"worker-{index}", system_prompt=WORKER_PROMPT, model_id=sub_model_id(ctx),
                tools=[ctx.env_tool(name) for name in WORKER_TOOLS],
            )
            rows = _parse_rows(str(worker("订单：" + ", ".join(batch))))
            ctx.trace.feature("worker_done", worker=index, orders=len(batch),
                              outcomes={r.get("outcome"): sum(x.get("outcome") == r.get("outcome") for x in rows)
                                        for r in rows})
            return rows

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = [row for rows in pool.map(work, range(1, len(batches) + 1), batches) for row in rows]
        missing = sorted(set(ids) - {r.get("order_id") for r in results})
        results += [{"order_id": oid, "outcome": "failed", "detail": "worker did not report"} for oid in missing]
        return "\n".join(json.dumps(r, ensure_ascii=False) for r in results)

    spec.tools.append(process_orders)
    spec.system_prompt += GUIDE


def _parse_rows(text: str) -> list[dict]:
    rows = []
    for match in re.finditer(r"\{[^{}]*\}", text):
        try:
            rows.append(json.loads(match.group(0)))
        except json.JSONDecodeError:
            continue
    return rows
