"""L3.5 · Structured notes.

A ``notes`` tool appends confirmed facts and evidence to ``reports/notes.md``.
Notes live outside the conversation, so they survive compaction verbatim (the
compactor re-injects them) and can be re-read at any time.
"""

from __future__ import annotations

from pathlib import Path

from strands import tool

from core.lab import AgentSpec, RunContext

GUIDE = """
## 笔记
- 每确认一个事实（时间、服务、配置项、日志原文），用 notes 工具记下来；排除一个假设也记下来。
- 对话可能被压缩，笔记会原文保留。写摘要时以笔记为准。
"""


def notes_path(ctx: RunContext) -> Path:
    return ctx.reports_dir / "notes.md"


def install(spec: AgentSpec, ctx: RunContext) -> None:
    path = notes_path(ctx)

    @tool
    def notes(action: str, text: str = "") -> str:
        """Keep investigation notes. action: "add" appends text (a confirmed fact, its evidence, or a
        ruled-out hypothesis); "read" returns all notes."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if action == "add":
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"- {text.strip()}\n")
            count = len(path.read_text(encoding="utf-8").splitlines())
            ctx.trace.feature("notes", action="add", entries=count, text=text[:200])
            return f"已记录（共 {count} 条）"
        content = path.read_text(encoding="utf-8") if path.exists() else "（暂无笔记）"
        ctx.trace.feature("notes", action="read", chars=len(content))
        return content

    spec.tools.append(notes)
    spec.system_prompt += GUIDE
