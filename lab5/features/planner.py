"""L5.2 · Plan-then-execute.

Before the first model call, a separate planner agent (fresh context, no
tools) turns the task into steps with completion criteria. The plan seeds the
todo list and is prepended to the task, so execution starts from an explicit
plan. Re-planning during execution is handled by the todo feature (L5.1).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from strands.hooks import BeforeInvocationEvent, HookProvider, HookRegistry

from core.lab import AgentSpec, RunContext, helper_agent
from lab5.features.common import sub_model_id
from lab5.features.todo import render

PLANNER_PROMPT = (
    "你是规划器。把任务拆成 4–10 个按顺序执行的步骤。每一步写清楚做什么，以及怎样判断这一步已经完成。"
    "考虑步骤之间的依赖，以及执行中可能出现的分支（例如某个操作失败后改走另一条路）。不要执行任何操作。"
)


class Step(BaseModel):
    content: str = Field(description="这一步要做什么")
    done_when: str = Field(description="怎样判断这一步已经完成")


class Plan(BaseModel):
    steps: list[Step]


class PlanFirst(HookProvider):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.planned = False

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._before_invocation)

    def _before_invocation(self, event: BeforeInvocationEvent) -> None:
        if self.planned or not event.messages:
            return
        self.planned = True
        planner = helper_agent(self.ctx, label="planner", system_prompt=PLANNER_PROMPT,
                               model_id=sub_model_id(self.ctx))
        plan: Plan = planner(self.ctx.instruction, structured_output_model=Plan).structured_output
        items = [{"content": f"{s.content}（完成标准：{s.done_when}）", "status": "pending", "note": ""}
                 for s in plan.steps]
        event.agent.state.set("todo", items)
        self.ctx.trace.feature("plan", steps=len(items), plan=[s.content for s in plan.steps])
        preface = {"text": "已为本任务制定计划，按计划执行，并随时用 todo_write 更新：\n" + render(items) + "\n\n"}
        first = event.messages[0]
        event.messages[0] = {**first, "content": [preface, *first.get("content", [])]}


def install(spec: AgentSpec, ctx: RunContext) -> None:
    spec.hooks.append(PlanFirst(ctx))
