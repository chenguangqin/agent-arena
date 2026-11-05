"""目标 2 · 压缩：手动 /compact 与自动压缩共用同一个 Compactor。

做法：把较早的消息交给一个独立的摘要 Agent，按固定模板写成摘要；
新的历史 = [第一条用户消息 + 摘要] + 最近几轮原文。

切分点总是选在一条 assistant 消息上，这样：
* 角色仍然是 user / assistant 交替；
* 工具调用（assistant 的 toolUse）和它的结果（下一条 user 的 toolResult）不会被拆开。

自动压缩：每次模型调用前估算上下文大小，超过阈值就压缩。上下文溢出时 Strands 会调用
reduce_context()，这里也用压缩来处理，而不是默认的"删掉旧消息"。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from strands import Agent
from strands.agent.conversation_manager import ConversationManager
from strands.hooks import BeforeModelCallEvent, HookRegistry

from labkit import build_model, trace
from solution.tokens import estimate

SUMMARY_PROMPT = """你负责压缩一段故障排查对话。只保留后续排查需要的信息，删去原始日志和重复内容，按下面的模板输出：

## 任务目标
## 已确认的事实（附关键证据：服务、时间、日志原文片段）
## 已排除的假设（及排除理由）
## 已执行的操作（配置修改、验证结果）
## 下一步
"""


def default_summarizer() -> Agent:
    return Agent(model=build_model(label="summarizer"), system_prompt=SUMMARY_PROMPT, callback_handler=None)


class Compactor(ConversationManager):
    def __init__(self, threshold_tokens: int | None = None, keep_recent: int = 4,
                 summarizer: Callable[[], Agent] = default_summarizer) -> None:
        super().__init__()
        self.threshold = threshold_tokens  # None: manual /compact only
        self.keep_recent = keep_recent
        self.summarizer = summarizer
        self.size_after_last = 0
        self.compactions = 0

    # -- automatic: before every model call ---------------------------------------------
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        super().register_hooks(registry, **kwargs)
        registry.add_callback(BeforeModelCallEvent, self._before_model)

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        if self.threshold is None:
            return
        size = estimate(event.agent.messages)
        # If the last compaction could not get under the threshold (one huge recent result),
        # wait until the context grows by another half threshold instead of compacting every call.
        if size >= self.threshold and size >= self.size_after_last + self.threshold // 2:
            self.compact(event.agent, reason=f"auto ({size} ≥ {self.threshold} tokens)")

    def apply_management(self, agent: Any, **kwargs: Any) -> None:
        return None

    def reduce_context(self, agent: Any, e: Exception | None = None, **kwargs: Any) -> None:
        if not self.compact(agent, reason="context overflow") and e is not None:
            raise e

    # -- the compaction itself -----------------------------------------------------------
    def split_point(self, messages: list[dict[str, Any]]) -> int | None:
        """Index of the first kept message: an assistant message about keep_recent from the end."""
        candidates = [i for i in range(2, len(messages)) if messages[i]["role"] == "assistant"]
        target = len(messages) - self.keep_recent
        before = [i for i in candidates if i <= target]
        return before[-1] if before else None

    def compact(self, agent: Any, focus: str | None = None, reason: str = "manual") -> bool:
        messages = agent.messages
        split = self.split_point(messages)
        if split is None:
            return False
        old = messages[1:split]
        transcript = json.dumps(old, ensure_ascii=False, default=str)[-120_000:]
        request = f"需要压缩的对话：\n{transcript}"
        if focus:
            request = f"压缩时务必保留：{focus}\n\n" + request
        summary = str(self.summarizer()(request)).strip()

        before_messages, before_tokens = len(messages), estimate(messages)
        first = messages[0]
        merged = {"role": "user", "content": [*first.get("content", []),
                                              {"text": f"\n\n[之前对话的压缩摘要]\n{summary}"}]}
        messages[:split] = [merged]
        self.removed_message_count += split - 1
        self.compactions += 1
        self.size_after_last = estimate(messages)
        trace.event("compaction", reason=reason, focus=focus, messages=f"{before_messages} → {len(messages)}",
                    tokens_est=f"{before_tokens} → {self.size_after_last}", summary=summary[:600])
        return True
