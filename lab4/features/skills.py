"""L4.1 · SOPs as skills, with the "SOP in the prompt" control group.

* ``sop_in_prompt`` (control): all three policy documents in the system prompt,
  every ticket, whether relevant or not.
* ``hooks``: progressive disclosure written by hand — the system prompt lists
  only each skill's name and description; ``load_skill(name)`` returns the body.
* ``sdk``: Strands ``AgentSkills`` does the same from the SKILL.md files.
"""

from __future__ import annotations

import re
from typing import Any

from strands import tool
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from core.lab import AgentSpec, RunContext
from lab4.features.common import current_ticket, state_dir


def read_skills(ctx: RunContext) -> dict[str, dict[str, str]]:
    skills = {}
    for path in sorted(state_dir(ctx, "skills").glob("*/SKILL.md")):
        if path.parent.name == "candidates":
            continue
        text = path.read_text(encoding="utf-8")
        match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", match.group(1), re.M)) if match else {}
        skills[meta.get("name", path.parent.name)] = {
            "description": meta.get("description", ""), "body": match.group(2) if match else text}
    return skills


def install_skills(spec: AgentSpec, ctx: RunContext) -> None:
    if ctx.config.get("skills", "mode") == "sdk":
        _install_sdk(spec, ctx)
        return
    skills = read_skills(ctx)
    listing = "\n".join(f"- {name}：{s['description']}" for name, s in skills.items())
    spec.system_prompt += (
        "\n\n## 可用技能\n处理工单前，先判断需要哪些技能，用 load_skill 加载后按其中的步骤操作。\n" + listing)

    @tool
    def load_skill(skill: str) -> str:
        """Load the full instructions of a skill listed under 可用技能."""
        if skill not in skills:
            raise ValueError(f"unknown skill {skill!r}; available: {sorted(skills)}")
        ctx.trace.feature("skill_loaded", skill=skill, ticket=current_ticket(ctx).get("id"), mode="hooks")
        return skills[skill]["body"]

    spec.tools.append(load_skill)


def _install_sdk(spec: AgentSpec, ctx: RunContext) -> None:
    from strands.vended_plugins.skills import AgentSkills

    sources = [str(p.parent) for p in sorted(state_dir(ctx, "skills").glob("*/SKILL.md"))
               if p.parent.name != "candidates"]
    spec.plugins.append(AgentSkills(skills=sources))
    spec.hooks.append(_SkillToolTracer(ctx))


class _SkillToolTracer(HookProvider):
    """Record which skill the SDK's ``skills`` tool activated."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        if event.tool_use.get("name") == "skills":
            self.ctx.trace.feature("skill_loaded", skill=(event.tool_use.get("input") or {}).get("skill_name"),
                                   ticket=current_ticket(self.ctx).get("id"), mode="sdk")


_POLICY_CACHE: dict[int, str] = {}


def install_sop_in_prompt(spec: AgentSpec, ctx: RunContext) -> None:
    """Control group: every policy document, in full, in the system prompt."""
    key = id(ctx)
    if key not in _POLICY_CACHE:
        names = [p["name"] for p in ctx.call_env("list_policies")]
        _POLICY_CACHE[key] = "\n\n".join(ctx.call_env("read_policy", policy=n) for n in names)
    spec.system_prompt += "\n\n## 公司政策（全文）\n" + _POLICY_CACHE[key]
    ctx.trace.feature("sop_in_prompt", chars=len(_POLICY_CACHE[key]), ticket=current_ticket(ctx).get("id"))
