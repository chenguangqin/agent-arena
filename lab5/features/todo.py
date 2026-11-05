"""L5.1 · Todo list with re-planning.

The model keeps its plan in a ``todo_write`` tool (full replacement, like a
checklist it rewrites). The plan lives in ``agent.state`` so harness code can
read it (the completion checks do), and ``ContextInjector`` shows the current
plan before every model call without writing it into history. Repeated tool
errors append a nudge to update the plan.
"""

from __future__ import annotations

from typing import Any

from strands import tool
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
from strands.types.tools import ToolContext

from core.lab import AgentSpec, RunContext
from lab5.features.common import append_to_result

STATUSES = ("pending", "in_progress", "done", "blocked")
GUIDE = """
## 计划管理
- 开始执行前，用 todo_write 把任务拆成可检查的步骤；每完成或改变一步都更新它。
- 状态只能是 pending / in_progress / done / blocked。遇到阻碍时把该步标为 blocked，写明原因，再调整计划。
"""


def render(items: list[dict[str, Any]]) -> str:
    if not items:
        return "（尚未制定计划）"
    marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]"}
    return "\n".join(
        f"{marks.get(i['status'], '[ ]')} {n}. {i['content']}" + (f" — {i['note']}" if i.get("note") else "")
        for n, i in enumerate(items, 1)
    )


def pending_items(state: Any) -> list[dict[str, Any]]:
    return [i for i in (state.get("todo") or []) if i.get("status") != "done"]


def install(spec: AgentSpec, ctx: RunContext) -> None:
    from strands.vended_plugins.context_injector import ContextInjector

    @tool(context=True)
    def todo_write(items: list[dict[str, str]], tool_context: ToolContext) -> str:
        """Replace the whole plan. Each item: {"content": str, "status": pending|in_progress|done|blocked,
        "note": optional str}. Send every item, including unchanged ones."""
        cleaned = []
        for item in items:
            status = item.get("status", "pending")
            if status not in STATUSES:
                raise ValueError(f"invalid status {status!r}; use one of {STATUSES}")
            cleaned.append({"content": str(item["content"]), "status": status, "note": item.get("note", "")})
        previous = tool_context.agent.state.get("todo") or []
        tool_context.agent.state.set("todo", cleaned)
        ctx.trace.feature("todo", items=len(cleaned), done=sum(i["status"] == "done" for i in cleaned),
                          blocked=sum(i["status"] == "blocked" for i in cleaned),
                          replanned=_structure_changed(previous, cleaned))
        return "计划已更新：\n" + render(cleaned)

    spec.tools.append(todo_write)
    spec.system_prompt += GUIDE
    spec.state.setdefault("todo", [])
    spec.plugins.append(
        ContextInjector(
            lambda c: f"<current_plan>\n{render(c.state.get('todo') or [])}\n</current_plan>",
            trigger="everyTurn",
        )
    )
    spec.hooks.append(ReplanNudge(ctx))


def _structure_changed(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> bool:
    return [i["content"] for i in old] != [i["content"] for i in new] and bool(old)


class ReplanNudge(HookProvider):
    """After two consecutive failures of the same tool, suggest updating the plan."""

    def __init__(self, ctx: RunContext, threshold: int = 2) -> None:
        self.ctx = ctx
        self.threshold = threshold
        self.streak: dict[str, int] = {}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use.get("name", "")
        if event.result.get("status") != "error":
            self.streak[name] = 0
            return
        self.streak[name] = self.streak.get(name, 0) + 1
        if self.streak[name] == self.threshold:
            self.ctx.trace.feature("replan_nudge", tool=name, failures=self.streak[name])
            event.result = append_to_result(
                event.result,
                f"[harness] {name} 已连续失败 {self.streak[name]} 次。先判断原因：是暂时故障（稍后重试、先做别的步骤），"
                "还是这条路走不通（换方法）。然后用 todo_write 更新计划。",
            )
