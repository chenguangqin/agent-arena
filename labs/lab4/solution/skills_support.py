"""目标 1 · Skill：用 Strands AgentSkills 按需加载政策。

三级按需加载：
1. 元数据：每个 skill 的 name 和 description 常驻 system prompt（AgentSkills 自动注入）；
2. 正文：模型调用 ``skills`` 工具时才加载 SKILL.md 正文；
3. 资源：正文里提到的 references/ 等文件，用 ``read_skill_file`` 再按需读取。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from strands import tool
from strands.vended_plugins.skills import AgentSkills

from labkit import CommandContext, Session, Trajectory


def skill_dirs(skills_dir: Path) -> list[Path]:
    return sorted(p.parent for p in skills_dir.glob("*/SKILL.md"))


def skills_plugin(skills_dir: Path) -> AgentSkills:
    return AgentSkills(skills=[str(p) for p in skill_dirs(skills_dir)])


def make_read_skill_file(skills_dir: Path) -> Any:
    root = skills_dir.resolve()

    @tool
    def read_skill_file(path: str) -> str:
        """读取 skill 附带的资源文件，path 相对于 skills 目录，例如
        "compensation-policy/references/compensation_table.md"。"""
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            raise ValueError(f"{path} 不是 skills 目录下的文件")
        return target.read_text(encoding="utf-8")

    return read_skill_file


def activated_skills(trajectory: Trajectory | None) -> list[str]:
    """Skills the agent loaded in a session (from its ``skills`` tool calls)."""
    if trajectory is None:
        return []
    return [(c["input"] or {}).get("skill_name", "?") for c in trajectory.tool_calls if c["name"] == "skills"]


def show_skills(arg: str, ctx: CommandContext) -> None:
    """/skills — list skills and what the last session loaded."""
    for path in skill_dirs(ctx.skills_dir):
        resources = [str(p.relative_to(path)) for p in path.rglob("*") if p.is_file() and p.name != "SKILL.md"]
        print(f"  - {path.name}" + (f"（资源：{', '.join(resources)}）" if resources else ""))
    last: Session | None = ctx.last_session
    if last is not None:
        print(f"上一个会话 {last.ticket['id']} 加载了：{activated_skills(last.trajectory) or '无'}")
