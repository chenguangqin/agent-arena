"""L3.4 · Compaction (+ L3.5 notes re-injection).

hooks: ``TemplateCompactor`` is a ConversationManager that checks the
projected context size before every model call. Above the threshold it asks a
separate summariser agent to condense the older messages into a fixed
template (goal, confirmed facts, ruled-out hypotheses, next steps), keeps the
task message and the most recent turns, and — when notes (L3.5) are enabled —
appends the notes file so confirmed evidence survives verbatim.

sdk: Strands' ``SummarizingConversationManager`` with proactive compression at
the equivalent ratio of the model's context window.
"""

from __future__ import annotations

import json
from typing import Any

from strands.agent.conversation_manager import ConversationManager
from strands.agent.conversation_manager.compression.context_compression import adjust_split_point_for_tool_pairs
from strands.hooks import BeforeModelCallEvent, HookRegistry

from core.lab import AgentSpec, RunContext, helper_agent
from lab3.features.notes import notes_path

CONTEXT_WINDOW = 200_000  # Haiku 4.5 on Bedrock; used to convert the token threshold into a ratio (sdk)
KEEP_RECENT = 6  # sdk: messages always preserved
KEEP_FRACTION = 0.4  # hooks: recent turns kept within 40% of the threshold
SUMMARY_PROMPT = """你负责压缩一段故障排查对话。按下面的模板输出，只保留后续排查需要的信息，删去原始日志和重复内容：

## 任务目标
## 已确认的事实（附关键证据：服务、时间、日志原文片段）
## 已排除的假设（及排除理由）
## 已执行的操作（配置修改、验证结果）
## 下一步
"""


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False)) // 3


class TemplateCompactor(ConversationManager):
    def __init__(self, ctx: RunContext, threshold_tokens: int) -> None:
        super().__init__()
        self.ctx = ctx
        self.threshold = threshold_tokens
        self.compactions = 0
        self.last_after = 0  # estimated size right after the previous compaction

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        super().register_hooks(registry, **kwargs)
        registry.add_callback(BeforeModelCallEvent, self._before_model)

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        # The SDK's projection is derived from the previous call's usage and can lag behind large
        # tool results; take the larger of it and a character-based estimate.
        projected = max(event.projected_input_tokens or 0, estimate_tokens(event.agent.messages))
        # Hysteresis: if the last compaction could not get below the threshold (e.g. one huge
        # tool result), wait until the context has grown by half a threshold before trying again.
        if projected >= self.threshold and projected >= self.last_after + self.threshold // 2:
            self.compact(event.agent, projected)

    def apply_management(self, agent: Any, **kwargs: Any) -> None:
        return None  # compaction happens before model calls; nothing to do after an invocation

    def reduce_context(self, agent: Any, e: Exception | None = None, **kwargs: Any) -> None:
        if not self.compact(agent, estimate_tokens(agent.messages)) and e is not None:
            raise e

    def compact(self, agent: Any, projected: int) -> bool:
        messages = agent.messages
        if len(messages) <= 4:
            return False
        # Keep recent turns by token budget, not by count: a few large tool results can
        # exceed the threshold on their own, which would trigger compaction on every call.
        split, kept = len(messages), 0
        while split > 3 and kept + estimate_tokens(messages[split - 1:split]) <= self.threshold * KEEP_FRACTION:
            split -= 1
            kept += estimate_tokens(messages[split:split + 1])
        split = min(split, len(messages) - 2)
        try:
            split = adjust_split_point_for_tool_pairs(messages, split)
        except Exception:
            return False
        old = messages[1:split]
        if len(old) < 2 or estimate_tokens(old) < self.threshold // 4:
            return False  # not worth a summariser call
        summariser = helper_agent(self.ctx, label="summariser", system_prompt=SUMMARY_PROMPT)
        transcript = json.dumps(old, ensure_ascii=False)[-120_000:]
        summary = str(summariser(f"任务：{self.ctx.instruction}\n\n需要压缩的对话：\n{transcript}")).strip()
        notes = notes_path(self.ctx)
        if self.ctx.config.enabled("context", "notes") and notes.exists():
            summary += "\n\n## 笔记（原文保留）\n" + notes.read_text(encoding="utf-8")
        before_messages, before_tokens = len(messages), estimate_tokens(messages)
        messages[1:split] = [
            {"role": "user", "content": [{"text": f"[之前对话的压缩摘要]\n{summary}"}]},
            {"role": "assistant", "content": [{"text": "好的，我会基于这份摘要继续。"}]},
        ]
        self.compactions += 1
        self.removed_message_count += len(old)
        self.last_after = estimate_tokens(messages)
        self.ctx.trace.feature("compaction", impl="hooks", round=self.compactions, projected_tokens=projected,
                               before_messages=before_messages, after_messages=len(messages),
                               before_tokens_est=before_tokens, after_tokens_est=estimate_tokens(messages),
                               summary=summary[:800])
        return True


def install(spec: AgentSpec, ctx: RunContext) -> None:
    threshold = ctx.config.get("context", "compaction_tokens")
    if ctx.config.get("context", "compaction") == "hooks":
        spec.conversation_manager = TemplateCompactor(ctx, threshold)
        return
    from strands.agent.conversation_manager import SummarizingConversationManager

    ratio = min(max(threshold / CONTEXT_WINDOW, 0.05), 1.0)
    spec.conversation_manager = SummarizingConversationManager(
        summary_ratio=0.5, preserve_recent_messages=KEEP_RECENT, proactive_compression={"compression_threshold": ratio})
    ctx.trace.feature("compaction_sdk", compression_threshold=round(ratio, 3))
