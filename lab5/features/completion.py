"""L5.3–L5.5 · Completion gate.

When the agent stops, the gate decides whether it is really done, in order of
cost: (1) did it call ``finish``? (2) deterministic checks against the
environment and the plan, (3) an independent judge that sees only the task,
the criteria, and the evidence — never the transcript. A failed layer sends
the missing items back as a new user message via ``AfterInvocationEvent.resume``.

``Limits`` cap a single invocation, so the gate also enforces a run-wide
round limit and the token budget before resuming.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field
from strands import tool
from strands.hooks import AfterInvocationEvent, HookProvider, HookRegistry
from strands.types.tools import ToolContext

from core.lab import AgentSpec, RunContext, helper_agent
from lab5.features.common import stop_loop, sub_model_id
from lab5.features.todo import pending_items, render

FINISH_GUIDE = """
## 结束任务
- 所有要求都完成并核实后，调用 finish 工具结束任务。直接回复文字不会被视为完成。
- evidence 写可以核对的事实（数量、编号、工具返回的结果），unresolved 如实写出未完成或不确定的事项。
"""

Check = Callable[[RunContext, Any], list[str]]


class Verdict(BaseModel):
    passed: bool = Field(description="所有完成标准是否都已满足")
    missing: list[str] = Field(default_factory=list, description="未满足的标准，逐条写出缺什么")


JUDGE_PROMPT = (
    "你是独立的验收员。你只能看到任务、完成标准和执行者提交的证据，看不到执行过程。"
    "逐条核对完成标准：证据缺失或前后矛盾（例如数量对不上）的，按未满足处理；"
    "不要因为执行者声称完成就判定通过。只要求与标准直接相关的证据（数量、编号、状态），"
    "不要求逐条日志或超出任务范围的材料。missing 中写清楚缺什么，以及执行者可以怎样补充。"
)


class CompletionGate(HookProvider):
    """Shared by the three layers; each feature switches its layer on."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.require_finish = False
        self.checks: list[Check] = []
        self.judge = False
        self.max_rounds = ctx.config.get("completion", "max_rounds")
        self.rounds = 0

    @classmethod
    def of(cls, spec: AgentSpec, ctx: RunContext) -> "CompletionGate":
        for hook in spec.hooks:
            if isinstance(hook, cls):
                return hook
        gate = cls(ctx)
        spec.hooks.append(gate)
        return gate

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterInvocationEvent, self._after_invocation)

    def _after_invocation(self, event: AfterInvocationEvent) -> None:
        agent = event.agent
        if agent.state.get("stalled"):
            self.ctx.trace.feature("gate", layer="stall", passed=False, action="stop (stalled)")
            return
        failure = self._evaluate(agent)
        if failure is None:
            self.ctx.trace.feature("gate", layer="all", passed=True, rounds=self.rounds)
            return
        layer, missing = failure
        if self.rounds >= self.max_rounds or self.ctx.budget.exhausted():
            self.ctx.trace.feature("gate", layer=layer, passed=False, missing=missing,
                                   action="give up (round or token limit)")
            return
        self.rounds += 1
        agent.state.set("finish", None)  # must call finish again after fixing
        self.ctx.trace.feature("gate", layer=layer, passed=False, missing=missing,
                               action="resume", round=self.rounds)
        event.resume = (
            f"[completion gate · 第 {self.rounds} 轮] 任务尚未完成，请处理以下问题后再调用 finish：\n"
            + "\n".join(f"- {m}" for m in missing)
            + "\n已经完成的操作不要重复执行；需要补充证据时，用工具查询后在 finish 中写出摘要（数量、编号）。"
        )

    def _evaluate(self, agent: Any) -> tuple[str, list[str]] | None:
        finish = agent.state.get("finish")
        if self.require_finish and not finish:
            return "finish", ["你没有调用 finish。确认所有要求都已完成后调用 finish；如果还没完成，继续执行。"]
        if self.checks:
            problems = [p for check in self.checks for p in check(self.ctx, agent)]
            if problems:
                return "checks", problems
        if self.judge:
            verdict = self._judge(agent, finish or {})
            if not verdict.passed:
                return "judge", verdict.missing or ["验收员判定未完成"]
        return None

    def _judge(self, agent: Any, finish: dict[str, Any]) -> Verdict:
        evidence = {
            "finish": finish,
            "plan": render(agent.state.get("todo") or []),
            "final_reply": _last_text(agent),
        }
        judge = helper_agent(self.ctx, label="judge", system_prompt=JUDGE_PROMPT,
                             model_id=sub_model_id(self.ctx))
        prompt = (
            f"## 任务与完成标准\n{self.ctx.instruction}\n\n"
            f"## 执行者提交的证据\n{json.dumps(evidence, ensure_ascii=False, indent=2)}"
        )
        verdict: Verdict = judge(prompt, structured_output_model=Verdict).structured_output
        self.ctx.trace.feature("judge", passed=verdict.passed, missing=verdict.missing)
        return verdict


def _last_text(agent: Any) -> str:
    for message in reversed(agent.messages):
        if message.get("role") == "assistant":
            texts = [b["text"] for b in message.get("content", []) if isinstance(b.get("text"), str)]
            if texts:
                return "\n".join(texts)[-2000:]
    return ""


# ---- L5.3 finish tool --------------------------------------------------------

def install_finish(spec: AgentSpec, ctx: RunContext) -> None:
    @tool(context=True)
    def finish(summary: str, evidence: str, unresolved: str, tool_context: ToolContext) -> str:
        """Declare the task complete. summary: what was done; evidence: verifiable facts
        (counts, ids, tool results); unresolved: anything not done or uncertain ("无" if none)."""
        tool_context.agent.state.set("finish", {"summary": summary, "evidence": evidence,
                                                "unresolved": unresolved})
        ctx.trace.feature("finish", summary=summary[:300], unresolved=unresolved[:300])
        stop_loop(tool_context.invocation_state)
        return "已提交，等待完成检查。"

    spec.tools.append(finish)
    spec.system_prompt += FINISH_GUIDE
    CompletionGate.of(spec, ctx).require_finish = True


# ---- L5.4 deterministic checks -------------------------------------------------

def check_plan(ctx: RunContext, agent: Any) -> list[str]:
    if not ctx.config.enabled("planning", "todo"):
        return []
    return [f"计划中仍有未完成的步骤：{i['content']}（{i['status']}）" for i in pending_items(agent.state)]


def check_environment(ctx: RunContext, agent: Any) -> list[str]:
    """Query the environment the way a test suite would; the model never sees these calls."""
    from devstore.scenarios.lab5_promo_incident import REPORT_SECTIONS

    problems = []
    rules = ctx.call_env("list_discount_rules")
    broken = [r["id"] for r in rules if r["max_discount_pct"] > 90]
    if broken:
        problems.append(f"折扣规则 {', '.join(broken)} 仍配置错误")
    failed, page = 0, 1
    while True:
        result = ctx.call_env("list_failed_orders", page=page)
        failed += sum(o["status"] in ("failed", "replay_failed") for o in result["orders"])
        if not result["has_more"]:
            break
        page += 1
    if failed:
        problems.append(f"还有 {failed} 个受影响订单既没有重放成功也没有退款")
    open_tickets = [t["id"] for t in ctx.call_env("list_tickets", status="open") if "大促" in t["subject"]]
    if open_tickets:
        problems.append(f"相关工单仍未关闭：{', '.join(open_tickets)}")
    report = ctx.call_env("get_report")
    if not report["exists"]:
        problems.append("还没有保存复盘报告")
    else:
        missing = [s for s in REPORT_SECTIONS if s not in report["markdown"]]
        if missing:
            problems.append(f"复盘报告缺少章节：{'、'.join(missing)}")
    return problems


def install_checks(spec: AgentSpec, ctx: RunContext) -> None:
    CompletionGate.of(spec, ctx).checks.extend([check_plan, check_environment])


# ---- L5.5 independent judge -------------------------------------------------------

def install_judge(spec: AgentSpec, ctx: RunContext) -> None:
    mode = ctx.config.get("completion", "judge")
    if mode == "hooks":
        CompletionGate.of(spec, ctx).judge = True
        return
    # sdk: Strands' GoalLoop runs its own judge agent and resumes on failure.
    from strands.vended_plugins.goal import GoalLoop, JudgeConfig

    spec.plugins.append(
        GoalLoop(
            goal=ctx.instruction,
            judge=JudgeConfig(model=ctx.model(sub_model_id(ctx))),
            max_attempts=ctx.config.get("completion", "max_rounds") + 1,
        )
    )
    ctx.trace.feature("goal_loop", max_attempts=ctx.config.get("completion", "max_rounds") + 1)
