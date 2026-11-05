"""L3.2 · Offload large tool results.

hooks: an ``AfterToolCallEvent`` hook writes any result longer than the
threshold to ``reports/offload/``, replaces it in context with a short
preview plus the file path, and gives the model two tools to pull back only
what it needs (a line range, or lines matching a pattern).

sdk: Strands' ``ContextOffloader`` plugin does the same with its own storage
and retrieval tool.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from strands import tool
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from core.lab import AgentSpec, RunContext
from core.trace import result_text

PREVIEW_HEAD, PREVIEW_TAIL = 15, 5
OWN_TOOLS = {"read_offloaded", "grep_offloaded"}


class Offloader(HookProvider):
    def __init__(self, ctx: RunContext, threshold: int) -> None:
        self.ctx = ctx
        self.threshold = threshold
        self.directory = ctx.reports_dir / "offload"
        self.count = 0

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use.get("name", "")
        if name in OWN_TOOLS or event.result.get("status") == "error":
            return
        text = result_text(event.result)
        if len(text) <= self.threshold:
            return
        self.count += 1
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{self.count:03d}-{name}.txt"
        path.write_text(text, encoding="utf-8")
        lines = text.splitlines()
        preview = lines[:PREVIEW_HEAD] + (["…"] if len(lines) > PREVIEW_HEAD + PREVIEW_TAIL else []) + \
            (lines[-PREVIEW_TAIL:] if len(lines) > PREVIEW_HEAD else [])
        ref = path.name
        replacement = (
            f"[offloaded] 结果共 {len(lines)} 行、{len(text)} 字符，已保存为 {ref}。以下是开头和结尾的预览。"
            f"需要细节时用 grep_offloaded(\"{ref}\", pattern) 查找，或用 read_offloaded(\"{ref}\", start, end) 读取行范围。\n"
            + "\n".join(preview)
        )
        event.result = {**{k: v for k, v in event.result.items() if k != "structuredContent"},
                        "content": [{"text": replacement}]}
        self.ctx.trace.feature("offload", tool=name, file=ref, lines=len(lines), before_chars=len(text),
                               after_chars=len(replacement))

    def resolve(self, ref: str) -> Path:
        path = (self.directory / Path(ref).name).resolve()
        if path.parent != self.directory.resolve() or not path.is_file():
            raise FileNotFoundError(f"no offloaded result named {ref!r}")
        return path


def install(spec: AgentSpec, ctx: RunContext) -> None:
    threshold = ctx.config.get("context", "offload_threshold")
    if ctx.config.get("context", "offload") == "sdk":
        from strands.vended_plugins.context_offloader import ContextOffloader

        spec.plugins.append(ContextOffloader(max_result_tokens=max(threshold // 4, 200), preview_tokens=300))
        ctx.trace.feature("offload_sdk", max_result_tokens=max(threshold // 4, 200))
        return
    offloader = Offloader(ctx, threshold)
    spec.hooks.append(offloader)

    @tool
    def read_offloaded(ref: str, start_line: int = 1, end_line: int = 100) -> str:
        """Read lines start_line..end_line (1-based, at most 200 lines) of an offloaded result."""
        lines = offloader.resolve(ref).read_text(encoding="utf-8").splitlines()
        end_line = min(end_line, start_line + 199, len(lines))
        ctx.trace.feature("offload_read", file=ref, start=start_line, end=end_line)
        return "\n".join(f"{n}: {lines[n - 1]}" for n in range(max(start_line, 1), end_line + 1))

    @tool
    def grep_offloaded(ref: str, pattern: str, limit: int = 50) -> str:
        """Lines of an offloaded result matching a regular expression (case-insensitive), with line numbers."""
        lines = offloader.resolve(ref).read_text(encoding="utf-8").splitlines()
        regex = re.compile(pattern, re.I)
        hits = [f"{n}: {line}" for n, line in enumerate(lines, 1) if regex.search(line)]
        ctx.trace.feature("offload_grep", file=ref, pattern=pattern, matches=len(hits))
        shown = hits[: max(1, min(limit, 200))]
        return f"# {len(hits)} matches" + (f", first {len(shown)} shown" if len(hits) > len(shown) else "") + \
            "\n" + "\n".join(shown)

    spec.tools.extend([read_offloaded, grep_offloaded])
