"""L5.8 · Stall detection.

Tracks consecutive identical calls (same tool, same arguments, same result).
Escalates in three steps: a hint to change approach, a request to re-plan,
then stopping the loop with a progress report instead of spinning.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from core.lab import AgentSpec, RunContext
from core.trace import result_text
from lab5.features.common import append_to_result, stop_loop


class StallGuard(HookProvider):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.threshold = ctx.config.get("guard", "threshold")
        self.last: str | None = None
        self.repeats = 0

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use.get("name", "")
        payload = json.dumps([name, event.tool_use.get("input"), result_text(event.result)],
                             sort_keys=True, ensure_ascii=False, default=str)
        signature = hashlib.sha1(payload.encode()).hexdigest()
        self.repeats = self.repeats + 1 if signature == self.last else 1
        self.last = signature
        level = self.repeats - self.threshold + 1  # 1, 2, 3 ...
        if level == 1:
            note = (f"[harness] 你已连续 {self.repeats} 次以相同参数调用 {name}，结果没有变化。"
                    "换一种方法，或者先做其他步骤。")
        elif level == 3:
            note = f"[harness] 仍在重复调用 {name}。停下来重新规划：说明卡在哪里，列出可行的替代方案。"
        elif level >= 5:
            event.agent.state.set("stalled", True)
            stop_loop(event.invocation_state)
            note = "[harness] 检测到停滞，已停止执行。请汇报当前进度、已完成和未完成的事项。"
        else:
            return
        self.ctx.trace.feature("stall", tool=name, repeats=self.repeats, level=level)
        event.result = append_to_result(event.result, note)


def install(spec: AgentSpec, ctx: RunContext) -> None:
    spec.hooks.append(StallGuard(ctx))
