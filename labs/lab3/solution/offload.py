"""目标 3 · 大结果卸载。

工具结果超过阈值时，完整内容写入 reports/offload/ 下的文件，上下文里只留预览（开头和结尾几行、
总行数）和文件路径。模型需要细节时，用 read_offloaded / grep_offloaded 按行或按关键词读取。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from strands import tool
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from labkit import trace

OFFLOAD_DIR = Path("reports") / "offload"
THRESHOLD_CHARS = 8_000
HEAD, TAIL = 15, 5


def _text(result: dict[str, Any]) -> str:
    return "\n".join(block.get("text", "") for block in result.get("content", []))


class Offload(HookProvider):
    def __init__(self, directory: Path = OFFLOAD_DIR, threshold: int = THRESHOLD_CHARS) -> None:
        self.directory = directory
        self.threshold = threshold
        self.count = 0

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        text = _text(event.result)
        name = event.tool_use.get("name", "")
        if len(text) <= self.threshold or name in ("read_offloaded", "grep_offloaded"):
            return
        self.count += 1
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{self.count:03d}-{name}.txt"
        path.write_text(text, encoding="utf-8")
        lines = text.splitlines()
        preview = "\n".join(lines[:HEAD] + (["…"] if len(lines) > HEAD + TAIL else []) + lines[-TAIL:])
        note = (f"[结果过大，已卸载] 共 {len(lines)} 行、{len(text)} 字符，完整内容在 {path}。\n"
                f"预览（开头 {HEAD} 行与结尾 {TAIL} 行）：\n{preview}\n"
                f"需要细节时用 grep_offloaded(path, pattern) 按关键词查找，或 read_offloaded(path, start, count) 按行读取。")
        trace.event("offload", tool=name, chars=f"{len(text)} → {len(note)}", path=str(path))
        result = {k: v for k, v in event.result.items() if k != "structuredContent"}
        event.result = {**result, "content": [{"text": note}]}


def _safe(path: str, directory: Path) -> Path:
    resolved = Path(path).resolve()
    if directory.resolve() not in resolved.parents:
        raise ValueError(f"只能读取 {directory} 下卸载的文件")
    return resolved


def make_offload_readers(directory: Path = OFFLOAD_DIR) -> list[Any]:
    @tool
    def read_offloaded(path: str, start: int = 1, count: int = 50) -> str:
        """按行读取一个已卸载的工具结果文件。start 从 1 开始，count 最多 200 行。"""
        lines = _safe(path, directory).read_text(encoding="utf-8").splitlines()
        start = max(start, 1)
        chunk = lines[start - 1:start - 1 + max(1, min(count, 200))]
        return f"# {path} 第 {start}-{start + len(chunk) - 1} 行，共 {len(lines)} 行\n" + "\n".join(chunk)

    @tool
    def grep_offloaded(path: str, pattern: str, max_matches: int = 30) -> str:
        """在一个已卸载的文件中按正则表达式查找，返回匹配行（带行号）和总匹配数。"""
        lines = _safe(path, directory).read_text(encoding="utf-8").splitlines()
        regex = re.compile(pattern, re.I)
        hits = [f"{n}: {line}" for n, line in enumerate(lines, 1) if regex.search(line)]
        shown = hits[: max(1, min(max_matches, 100))]
        return f"# {len(hits)} 处匹配（显示 {len(shown)} 处）\n" + "\n".join(shown)

    return [read_offloaded, grep_offloaded]
