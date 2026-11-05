"""L4.3 · Lessons from QA feedback.

After each ticket the harness reads the QA feedback. Feedback that states a
rule is distilled into ``memory/lessons.md`` (with its source ticket and time;
a new rule that contradicts an old one marks the old one as superseded).
Every later ticket starts with the active rules in its first message.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent, HookProvider, HookRegistry

from core.lab import AgentSpec, RunContext, helper_agent
from lab4.features.common import current_ticket, prepend_to_first_message, state_dir

DISTILL_PROMPT = (
    "把客服质检意见提炼成一条以后处理工单时可以直接执行的规则，一句话，祈使句。"
    "如果它与已有规则冲突，给出被替代规则的序号；否则 supersedes 为 null。"
    "如果质检意见只是“合格”之类，没有可执行的规则，rule 留空。"
)


class Lesson(BaseModel):
    rule: str = Field(default="", description="可执行的规则；没有则为空")
    supersedes: int | None = Field(default=None, description="被替代的已有规则序号（从 1 开始）")


class LessonBook:
    def __init__(self, path: Path) -> None:
        self.path = path

    def lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []

    def active(self) -> list[str]:
        return [line for line in self.lines() if line.startswith("- ") and "（已被替代" not in line]

    def add(self, rule: str, source: str, supersedes: int | None) -> None:
        lines = self.lines()
        rules = [i for i, line in enumerate(lines) if line.startswith("- ")]
        if supersedes and 1 <= supersedes <= len(rules):
            lines[rules[supersedes - 1]] += f"（已被替代：{source}）"
        stamp = datetime.datetime.now().isoformat(timespec="minutes")
        lines.append(f"- [来源 {source} · {stamp}] {rule}")
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class LessonKeeper(HookProvider):
    def __init__(self, ctx: RunContext, book: LessonBook) -> None:
        self.ctx = ctx
        self.book = book

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._before)
        registry.add_callback(AfterInvocationEvent, self._after)

    def _before(self, event: BeforeInvocationEvent) -> None:
        active = self.book.active()
        if not active:
            return
        self.ctx.trace.feature("lessons_injected", ticket=current_ticket(self.ctx).get("id"), rules=len(active))
        prepend_to_first_message(event, "## 以往质检意见中得到的规则（必须遵守）\n" + "\n".join(active) + "\n\n")

    def _after(self, event: AfterInvocationEvent) -> None:
        ticket = current_ticket(self.ctx).get("id")
        try:
            feedback = self.ctx.call_env("get_feedback", ticket_id=ticket)["feedback"]
        except RuntimeError:
            return  # not submitted
        if "处理合格" in feedback:
            return
        existing = "\n".join(f"{n}. {line}" for n, line in enumerate(self.book.active(), 1)) or "（无）"
        distiller = helper_agent(self.ctx, label="lesson-distiller", system_prompt=DISTILL_PROMPT)
        lesson: Lesson = distiller(f"质检意见：{feedback}\n\n已有规则：\n{existing}",
                                   structured_output_model=Lesson).structured_output
        if not lesson.rule.strip():
            return
        self.book.add(lesson.rule.strip(), ticket, lesson.supersedes)
        self.ctx.trace.feature("lesson_saved", ticket=ticket, feedback=feedback, rule=lesson.rule,
                               supersedes=lesson.supersedes)


def install(spec: AgentSpec, ctx: RunContext) -> None:
    spec.hooks.append(LessonKeeper(ctx, LessonBook(state_dir(ctx, "memory") / "lessons.md")))
