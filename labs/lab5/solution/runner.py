"""The workflow runner: orchestrator → engine → completion check, with re-planning.

Exposes the same interface as a Strands Agent for labkit: ``runner(task) -> str``,
``cleanup()``, ``tool_names``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from strands import Agent

from labkit import Env, build_model, trace
from solution.checks import NotifyLedger, RefundGuard, TicketGuard, completion_problems
from solution.workflow import Engine, Step, StepFailed, Workflow, normalize, summarize, validate

ModelFactory = Callable[..., Any]  # build_model(label=...) -> model

ORCHESTRATOR_PROMPT = """你是工作流编排者。你不直接操作环境，而是设计工作流，由执行引擎交给子 Agent 完成。

工作流规则：
- 每个步骤交给一个上下文独立的子 Agent，它只能看到本步骤的指令、前置步骤结果的摘要和你分配的工具。
- 只给每个步骤完成它所需的最少工具。
- 互不依赖的步骤会并行执行；用 depends_on 表达先后顺序。
- agent 步骤要查询或修改环境时，必须在 tools 中分配对应的工具；没有工具的 agent 步骤只能基于前置步骤的结果推理或写作。
- 需要完整的数据列表（订单、工单）时，用 kind=tool 步骤由引擎直接调用查询工具（分页工具设 paginate=true），
  不要让子 Agent 抄写数据——模型会省略和截断长列表。
- 对每个条目做同样的单次查询时，用 foreach + tool（arguments 中用 {字段名} 引用条目字段），由引擎逐条调用。
  需要判断和分支的处理（例如重放失败后改为退款）用 foreach + instruction 交给子 Agent。
- 逐个处理大量条目时，用 foreach 步骤按批处理：items_from 指向产出列表的步骤，where 按字段过滤，
  unique_by 去重（例如每位客户只通知一次：unique_by=customer_id）。不要让一个 agent 步骤处理几十个条目。
- where 和 unique_by 使用的字段名必须和列表条目的实际字段一致；不确定时先看工具描述或前一轮的结果摘要。
- 后续轮次中，已完成的步骤可以直接作为依赖或 items_from 引用，不要重复执行。
- 先想清楚步骤之间的因果顺序：必须先完成的操作（例如先修复配置、再重试依赖它的操作）要写进 depends_on。

## 示例（另一个任务，仅用于说明格式）
任务：仓库系统故障导致一批退货申请卡住，需要修复后逐个处理并通知客户。
{"goal": "修复并处理卡住的退货申请",
 "steps": [
  {"id": "diagnose", "kind": "agent", "instruction": "查看告警和错误日志，找出导致退货卡住的配置项，返回配置项名称和正确值。",
   "tools": ["list_alerts", "search_logs"]},
  {"id": "fix", "kind": "agent", "instruction": "按诊断结果修复配置，返回修改前后的值。", "tools": ["update_config"],
   "depends_on": ["diagnose"]},
  {"id": "returns", "kind": "tool", "tool": "list_stuck_returns", "paginate": true},
  {"id": "process", "kind": "foreach", "items_from": "returns", "depends_on": ["returns", "fix"], "batch_size": 8,
   "instruction": "对每个退货申请：先 retry_return；如果失败且原因是商品已报废，改为 issue_refund。每个申请输出一行 JSON：{\"return_id\":..., \"customer_id\":..., \"outcome\": \"retried|refunded|failed\"}。",
   "tools": ["get_return", "retry_return", "issue_refund"]},
  {"id": "notify", "kind": "foreach", "items_from": "process", "depends_on": ["process"], "unique_by": "customer_id",
   "instruction": "按 outcome 通知客户处理结果，每行输出 {\"customer_id\":..., \"outcome\": \"notified\"}。", "tools": ["notify_customer"]},
  {"id": "report", "kind": "agent", "instruction": "根据前置步骤的结果写处理报告并保存。", "tools": ["save_report"],
   "depends_on": ["fix", "process", "notify"]}
 ]}"""

NODE_PROMPT = """你是工作流中的一个执行节点，在在线商店的事故处理环境中工作。
只完成本步骤的指令，直接用工具动手处理。每一轮最多发起 8 个工具调用，分批完成。
遇到暂时故障（如 503）稍后重试，最多 3 次。
如果本步骤无法完成，回复的第一行写 FAILED: 原因。"""

CONTINUE = "上一条回复超出了单次输出上限被截断，其中的工具调用没有执行。继续完成本步骤，每轮少发起一些工具调用。"


class WorkflowRunner:
    def __init__(self, env: Env, model_factory: ModelFactory = build_model, max_rounds: int = 5,
                 max_workers: int = 4) -> None:
        self.env = env
        self.model_factory = model_factory
        self.max_rounds = max_rounds
        self.ledger = NotifyLedger()
        self.refund_guard = RefundGuard()
        self.ticket_guard = TicketGuard()
        self.engine = Engine(self.run_node, call_tool=env.call, max_workers=max_workers)
        self.orchestrator = Agent(model=model_factory(label="orchestrator"), system_prompt=ORCHESTRATOR_PROMPT,
                                  hooks=[trace.Tracer("orchestrator")], callback_handler=None)
        self.catalog = env.tool_catalog()

    @property
    def tool_names(self) -> list[str]:
        return [t["name"] for t in self.catalog]

    def cleanup(self) -> None:
        self.orchestrator.cleanup()

    # ---- one workflow node -----------------------------------------------------------
    def run_node(self, step: Step, label: str, prompt: str) -> str:
        from strands.types.agent import Limits
        from strands.types.exceptions import MaxTokensReachedException

        tools = [self.env.mcp_client(tool_filters={"allowed": step.tools})] if step.tools else []
        node = Agent(model=self.model_factory(label=label), tools=tools, system_prompt=NODE_PROMPT,
                     hooks=[trace.Tracer(label), self.ledger, self.refund_guard, self.ticket_guard], callback_handler=None)
        try:
            message = prompt
            for attempt in range(3):
                try:
                    return str(node(message, limits=Limits(turns=40)))
                except MaxTokensReachedException:
                    trace.event("output_recovery", step=label, attempt=attempt + 1)
                    message = CONTINUE
            raise StepFailed(step.id, "子 Agent 的输出多次超出上限")
        finally:
            node.cleanup()

    # ---- the outer loop ----------------------------------------------------------------
    def __call__(self, task: str) -> str:
        results: dict[str, Any] = {}
        message = (f"## 任务\n{task}\n\n## 可用的环境工具\n{json.dumps(self.catalog, ensure_ascii=False, indent=1)}\n\n"
                   "请设计完成这个任务的工作流。")
        for round_no in range(1, self.max_rounds + 1):
            workflow = self._valid_plan(message, round_no, set(results))
            if workflow is None:
                return f"编排者连续给出无法执行的工作流。{self._progress(results)}"
            trace.event("workflow", round=round_no, goal=workflow.goal,
                        steps=[{"id": s.id, "kind": s.kind, "depends_on": s.depends_on, "tools": s.tools}
                               for s in workflow.steps])
            failure = self.engine.run(workflow, results)
            if failure is not None:
                message = (f"步骤 {failure.step} 失败：{failure.reason}\n\n{self._progress(results)}\n\n"
                           "请只为剩余的工作给出新的工作流（可以引用已完成步骤的 id）。")
                continue
            problems = completion_problems(self.env, self.ledger)
            trace.event("completion_check", round=round_no, passed=not problems, problems=problems)
            if not problems:
                return self._final(results, round_no)
            message = ("完成检查未通过：\n" + "\n".join(f"- {p}" for p in problems)
                       + f"\n\n{self._progress(results)}\n\n请给出补救这些问题的工作流（可以引用已完成步骤的 id）。")
        return f"未能在 {self.max_rounds} 轮内完成。{self._progress(results)}"

    def _valid_plan(self, message: str, round_no: int, done: set[str], attempts: int = 3) -> Workflow | None:
        """Ask for a workflow; invalid ones go straight back with the errors (not counted as rounds)."""
        for _ in range(attempts):
            workflow = normalize(self.plan(message))
            errors = validate(workflow, set(self.tool_names), done=done)
            if not errors:
                return workflow
            trace.event("workflow_invalid", round=round_no, errors=errors)
            message = "这个工作流不能执行：\n" + "\n".join(f"- {e}" for e in errors) + "\n请修正后重新给出完整的剩余工作流。"
        return None

    def plan(self, message: str) -> Workflow:
        workflow: Workflow = self.orchestrator(message, structured_output_model=Workflow).structured_output
        return workflow

    @staticmethod
    def _progress(results: dict[str, Any]) -> str:
        lines = [f"- {sid}: {summarize(out, 300)}" for sid, out in results.items()]
        return "已完成的步骤：\n" + ("\n".join(lines) if lines else "（无）")

    @staticmethod
    def _final(results: dict[str, Any], rounds: int) -> str:
        return f"工作流在第 {rounds} 轮完成，完成检查通过。共执行 {len(results)} 个步骤：{', '.join(results)}。"
