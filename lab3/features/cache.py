"""L3.7 · Prompt caching.

Marks the stable prefix (system prompt, tool definitions) for Bedrock prompt
caching via ``CacheConfig(strategy="auto")``. The trace and summary show
cache read/write tokens; nothing about the agent's behaviour changes.
"""

from __future__ import annotations

from core.lab import AgentSpec, RunContext


def install(spec: AgentSpec, ctx: RunContext) -> None:
    spec.cache = True
    ctx.trace.feature("prompt_cache", strategy="auto")
