"""Run-wide budget across every invocation, resume round, and helper agent.

``strands.types.agent.Limits`` caps a single invocation only; completion gates
that ``resume`` and sub-agents need a shared total.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    max_total_tokens: int = 0  # 0 = unlimited
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model_calls: int = 0
    peak_context_tokens: int = 0
    by_agent: dict[str, int] = field(default_factory=dict)

    def add(self, agent: str, usage: dict[str, int]) -> None:
        self.model_calls += 1
        self.input_tokens += usage.get("input", 0)
        self.output_tokens += usage.get("output", 0)
        self.cache_read_tokens += usage.get("cache_read", 0)
        self.cache_write_tokens += usage.get("cache_write", 0)
        context = usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0)
        self.peak_context_tokens = max(self.peak_context_tokens, context)
        self.by_agent[agent] = self.by_agent.get(agent, 0) + usage.get("input", 0) + usage.get("output", 0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def remaining(self) -> int | None:
        if not self.max_total_tokens:
            return None
        return max(self.max_total_tokens - self.total_tokens, 0)

    def exhausted(self) -> bool:
        return bool(self.max_total_tokens) and self.total_tokens >= self.max_total_tokens

    def as_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "model_calls": self.model_calls,
            "peak_context_tokens": self.peak_context_tokens,
            "max_total_tokens": self.max_total_tokens or None,
            "by_agent": dict(self.by_agent),
        }
