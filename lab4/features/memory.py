"""L4.2 · Fact memory across tickets.

Every ticket starts in a fresh context, so facts found while handling one
ticket — a customer's channel preference, a known gateway bug — are lost unless
written down.

* ``hooks``: ``memory/facts.md`` with ``remember`` / ``recall`` tools; at the
  start of each ticket, entries that match the ticket are injected into the
  first message, with the ticket they came from.
* ``sdk``: Strands ``MemoryManager`` with a ``FileMemoryStore`` under the same
  directory (search/add tools and automatic injection).
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from strands import tool
from strands.hooks import BeforeInvocationEvent, HookProvider, HookRegistry

from core.lab import AgentSpec, RunContext
from lab4.features.common import current_ticket, prepend_to_first_message, state_dir, tokens

GUIDE = """

## 记忆
- 每张工单都在新的会话中处理，之前工单里的信息只能通过记忆获得。
- 发现以后可能用得上的事实时，用 remember 记下来：客户的偏好（例如只接受某种联系渠道）、系统的已知问题和处理办法等。写清楚对象（客户编号、系统名称）。
- 不确定时可以用 recall 查询。
"""
ENTRY = re.compile(r"^- \[来源 (?P<source>[^ ]+) · (?P<time>[^\]]+)\] (?P<fact>.+?)(?: ｜ 关于：(?P<about>.+))?$")
MAX_INJECT = 6


class FactStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if match := ENTRY.match(line.strip()):
                rows.append({k: v or "" for k, v in match.groupdict().items()})
        return rows

    def add(self, fact: str, about: str, source: str) -> dict[str, str]:
        stamp = datetime.datetime.now().isoformat(timespec="minutes")
        line = f"- [来源 {source} · {stamp}] {fact.strip()} ｜ 关于：{about.strip()}"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return {"source": source, "time": stamp, "fact": fact, "about": about}

    def search(self, text: str, must: set[str] = frozenset(), limit: int = MAX_INJECT) -> list[dict[str, str]]:
        query = tokens(text)
        scored = []
        for entry in self.entries():
            words = tokens(entry["fact"] + " " + entry["about"])
            score = len(query & words) + (10 if must & words else 0)
            if score >= 2:
                scored.append((score, entry))
        return [e for _, e in sorted(scored, key=lambda x: -x[0])[:limit]]


def install_facts(spec: AgentSpec, ctx: RunContext) -> None:
    if ctx.config.get("memory", "facts") == "sdk":
        _install_sdk(spec, ctx)
        return
    store = FactStore(state_dir(ctx, "memory") / "facts.md")

    @tool
    def remember(fact: str, about: str) -> str:
        """Save a fact useful for future tickets. about: who/what it concerns (customer id, system name)."""
        entry = store.add(fact, about, current_ticket(ctx).get("id", "?"))
        ctx.trace.feature("memory_write", store="facts", ticket=entry["source"], fact=fact, about=about)
        return "已记住。"

    @tool
    def recall(query: str) -> list[dict]:
        """Search saved facts from earlier tickets."""
        hits = store.search(query)
        ctx.trace.feature("memory_recall", query=query, hits=len(hits))
        return hits

    spec.tools += [remember, recall]
    spec.system_prompt += GUIDE
    spec.hooks.append(_InjectFacts(ctx, store))


class _InjectFacts(HookProvider):
    def __init__(self, ctx: RunContext, store: FactStore) -> None:
        self.ctx = ctx
        self.store = store

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._before)

    def _before(self, event: BeforeInvocationEvent) -> None:
        ticket = current_ticket(self.ctx)
        text = " ".join(str(ticket.get(k, "")) for k in ("customer_id", "order_id", "subject", "body"))
        hits = self.store.search(text, must={ticket.get("customer_id", "")} - {""})
        if not hits:
            return
        self.ctx.trace.feature("memory_injected", store="facts", ticket=ticket.get("id"),
                               entries=[{"from": h["source"], "fact": h["fact"]} for h in hits])
        lines = "\n".join(f"- （来自 {h['source']}）{h['fact']}（关于：{h['about']}）" for h in hits)
        prepend_to_first_message(event, f"## 与本工单可能相关的记忆\n{lines}\n\n")


def _install_sdk(spec: AgentSpec, ctx: RunContext) -> None:
    from strands.memory import MemoryManager
    from strands.storage import LocalFileStorage
    from strands.vended_memory_stores.file_memory_store import FileMemoryStore

    base = state_dir(ctx, "memory") / "sdk"
    base.mkdir(parents=True, exist_ok=True)
    store = FileMemoryStore(name="facts", description="客户偏好、系统已知问题等跨工单事实",
                            storage=LocalFileStorage(base_dir=str(base)))
    spec.plugins.append(MemoryManager(stores=[store], add_tool_config=True, injection=True))
    spec.system_prompt += GUIDE.replace("remember", "add_memory").replace("recall", "search_memory")
    ctx.trace.feature("memory_manager", store="FileMemoryStore", path=str(base.relative_to(ctx.agent_root)))
