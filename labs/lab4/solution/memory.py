"""目标 2 · 长期记忆：采集 → 抽取 → 治理 → 使用。

* 采集：会话结束后保存轨迹（memory/trajectories/<工单>.json）。
* 抽取：用一个独立的模型调用，从轨迹中抽取候选记忆（事实、偏好、经验教训），
  每条带主题、置信度，以及它与已有记忆的关系。不依赖执行 Agent 主动"记住"。
* 治理：
  - 准入：置信度低于阈值的候选不写入（避免"已要求客户补充信息"这类无用记录）；
  - 去重：与已有记忆重复时合并来源、提高置信度，而不是追加；
  - 消歧：与已有记忆冲突或更新时，旧条目标为"已被替代"并指向新条目；
  - 遗忘：长期未被使用的低置信条目自动标为"已遗忘"；/forget 手动遗忘。
* 使用：新会话开始时检索相关记忆（触发词命中、主题匹配、内容相似度），注入 system prompt，并记录使用次数。
  触发词在写入时由抽取模型给出，覆盖客户的不同说法——检索质量在写入时就决定了大半。

记忆保存在 memory/MEMORY.md，人和模型都能直接阅读。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from strands import Agent

from labkit import CommandContext, Session, Trajectory, build_model, trace

MIN_CONFIDENCE = 0.6  # admission threshold
DUPLICATE_SIMILARITY = 0.55  # character-bigram Jaccard, same kind + subject
FORGET_AFTER_SESSIONS = 6  # unused for this many sessions …
FORGET_BELOW_CONFIDENCE = 0.75  # … and confidence below this → forgotten
MAX_INJECTED = 6
KIND_LABEL = {"fact": "事实", "preference": "偏好", "lesson": "经验"}
LABEL_KIND = {v: k for k, v in KIND_LABEL.items()}

# Tests replace this to drive the extractor with a scripted model.
MODEL_FACTORY: Callable[[], Any] = build_model


@dataclass
class Memory:
    id: str
    kind: str  # fact | preference | lesson
    subject: str  # 客户编号、系统名称；对所有工单适用的规则写"全局"
    content: str
    sources: list[str]
    created_session: int
    confidence: float
    uses: int = 0
    last_used_session: int = 0
    status: str = "active"  # active | superseded | forgotten
    note: str = ""  # 被替代/遗忘的原因
    keywords: list[str] = field(default_factory=list)  # 写入时建立的检索触发词

    @property
    def active(self) -> bool:
        return self.status == "active"


class MemoryStore:
    """Read and write memory/MEMORY.md."""

    HEADER = re.compile(r"^## (M-\d+) · (\S+) · (.+)$")
    FIELD = re.compile(r"^- (\S+?)：(.*)$")

    def __init__(self, memory_dir: Path) -> None:
        self.path = memory_dir / "MEMORY.md"
        self.memory_dir = memory_dir
        self.sessions = 0
        self.items: list[Memory] = []
        self.load()

    # ---- persistence ----------------------------------------------------------------
    def load(self) -> None:
        self.items, self.sessions = [], 0
        if not self.path.exists():
            return
        current: dict[str, Any] | None = None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if match := re.match(r"<!-- sessions: (\d+) -->", line):
                self.sessions = int(match.group(1))
            elif match := self.HEADER.match(line):
                current = {"id": match.group(1), "kind": LABEL_KIND.get(match.group(2), "fact"),
                           "subject": match.group(3), "content": "", "fields": {}}
                self.items.append(current)  # type: ignore[arg-type]
            elif current is not None and (match := self.FIELD.match(line)):
                current["fields"][match.group(1)] = match.group(2)
            elif current is not None and line.strip() and not current["content"]:
                current["content"] = line.strip()
        self.items = [self._from_raw(raw) for raw in self.items]  # type: ignore[arg-type]

    @staticmethod
    def _from_raw(raw: dict[str, Any]) -> Memory:
        f = raw["fields"]
        stats = dict(re.findall(r"(置信度|使用|最近使用|创建)=([\d.]+)", f.get("统计", "")))
        return Memory(id=raw["id"], kind=raw["kind"], subject=raw["subject"], content=raw["content"],
                      sources=[s for s in f.get("来源", "").split("、") if s],
                      created_session=int(stats.get("创建", 0)), confidence=float(stats.get("置信度", 0.5)),
                      uses=int(stats.get("使用", 0)), last_used_session=int(stats.get("最近使用", 0)),
                      status=f.get("状态", "active").split(" ")[0], note=f.get("状态", "").partition(" ")[2],
                      keywords=[k for k in f.get("触发词", "").split("、") if k])

    def save(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# 长期记忆", f"<!-- sessions: {self.sessions} -->", ""]
        for m in self.items:
            lines += [f"## {m.id} · {KIND_LABEL.get(m.kind, m.kind)} · {m.subject}", m.content,
                      f"- 来源：{'、'.join(m.sources)}",
                      f"- 触发词：{'、'.join(m.keywords)}",
                      f"- 统计：置信度={m.confidence:.2f} 使用={m.uses} 最近使用={m.last_used_session} "
                      f"创建={m.created_session}",
                      f"- 状态：{m.status}" + (f" {m.note}" if m.note else ""), ""]
        self.path.write_text("\n".join(lines), encoding="utf-8")

    # ---- governance -------------------------------------------------------------------
    def next_id(self) -> str:
        return f"M-{len(self.items) + 1:03d}"

    def get(self, memory_id: str | None) -> Memory | None:
        return next((m for m in self.items if m.id == memory_id), None)

    def find_duplicate(self, kind: str, subject: str, content: str) -> Memory | None:
        for m in self.items:
            if m.active and m.kind == kind and m.subject == subject and similarity(m.content, content) >= \
                    DUPLICATE_SIMILARITY:
                return m
        return None

    def add(self, kind: str, subject: str, content: str, source: str, confidence: float,
            keywords: list[str] | None = None) -> Memory:
        memory = Memory(self.next_id(), kind, subject, content, [source], self.sessions, confidence,
                        keywords=list(keywords or []))
        self.items.append(memory)
        return memory

    def forget_stale(self) -> list[Memory]:
        forgotten = []
        for m in self.items:
            idle = self.sessions - max(m.created_session, m.last_used_session)
            if m.active and m.uses == 0 and idle >= FORGET_AFTER_SESSIONS and m.confidence < FORGET_BELOW_CONFIDENCE:
                m.status, m.note = "forgotten", f"（{idle} 个会话未被使用）"
                forgotten.append(m)
        return forgotten

    def forget(self, keyword: str) -> list[Memory]:
        hits = [m for m in self.items if m.active and keyword and (keyword in m.content or keyword in m.subject)]
        for m in hits:
            m.status, m.note = "forgotten", "（手动 /forget）"
        return hits

    # ---- use ------------------------------------------------------------------------------
    def relevant(self, ticket: dict[str, Any] | None, limit: int = MAX_INJECTED) -> list[Memory]:
        """Lessons (rules for every ticket) always; facts and preferences only when they match this ticket."""
        active = [m for m in self.items if m.active]
        lessons = [m for m in active if m.kind == "lesson"]
        others = [m for m in active if m.kind != "lesson"]
        if ticket is None:
            return lessons + others[:limit]
        text = " ".join(str(ticket.get(k, "")) for k in ("customer_id", "order_id", "subject", "body"))
        scored = []
        for m in others:
            score = sum(1.0 for k in m.keywords if k and k in text)  # write-time triggers
            if m.subject in (ticket.get("customer_id"), ticket.get("order_id")):
                score += 1.0
            # either direction: most of the memory is in the ticket, or most of the ticket is in the memory
            score += max(similarity(m.content, text, overlap=True), similarity(text, m.content, overlap=True))
            if m.subject.lower() in text.lower():  # e.g. the text names the system "PG-B"
                score += 1.0
            if score >= 0.6:
                scored.append((score, m))
        return lessons + [m for _, m in sorted(scored, key=lambda pair: -pair[0])[:limit]]

def bigrams(text: str) -> set[str]:
    chars = re.sub(r"\s+", "", text.lower())
    return {chars[i:i + 2] for i in range(len(chars) - 1)}


def similarity(a: str, b: str, overlap: bool = False) -> float:
    """Character-bigram Jaccard (or overlap ratio against ``a``) — works for Chinese without a tokenizer."""
    x, y = bigrams(a), bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / (len(x) if overlap else len(x | y))


# ---- 抽取 --------------------------------------------------------------------------------

class Candidate(BaseModel):
    kind: Literal["fact", "preference", "lesson"] = Field(
        description="fact：系统或业务的事实与已知问题；preference：某位客户的偏好；lesson：质检反馈等以后都适用的做法")
    subject: str = Field(description="这条记忆关于谁/什么：客户编号（如 C5001）、系统名称（如 PG-B）；适用于所有工单的规则写'全局'")
    content: str = Field(description="一句话，具体、可执行：以后遇到什么情况、应该怎么做")
    confidence: float = Field(description="0–1。轨迹中有直接证据（工具结果、客户原话、质检意见）为高；推测为低")
    keywords: list[str] = Field(default_factory=list, description=(
        "以后的工单里可能出现、应当触发这条记忆的词：客户对同一现象的各种口语化说法，以及系统名称；"
        "客户偏好写客户编号"))
    relation: Literal["new", "duplicate", "update", "contradicts"] = Field(
        description="与已有记忆的关系：new 新信息；duplicate 与某条已有记忆相同；update 补充或修正某条；contradicts 与某条冲突")
    target_id: str | None = Field(default=None, description="relation 不是 new 时，对应的已有记忆编号")


class Extraction(BaseModel):
    items: list[Candidate] = Field(default_factory=list)


EXTRACT_PROMPT = """你负责从一次客服会话的轨迹中抽取长期记忆，供以后处理其他工单时使用。
只抽取以后的工单可能用得上、而且公司政策里没有写的信息：
- 系统或业务的事实、已知问题，以及**具体怎么查证、怎么处理**（用哪个工具、查询条件怎么写、证据在哪里）；
- 某位客户明确表达的偏好；
- 质检意见中提出的、以后都要遵守的做法（kind=lesson）。
不要抽取：公司政策中已有的通用规则（退款权限、补偿面额、升级条件等）、只与本工单有关的细节、对客户的回复内容、你的推测。
宁缺毋滥：一次会话通常只有 0–2 条值得记。

keywords 决定这条记忆以后能不能被找到：以后的客户不会用你的术语描述问题。
要覆盖客户描述同一现象时可能用的口语化、含糊的说法（至少 5 个，每个 2–6 个字），以及相关的系统名称。

如果和已有记忆重复、补充或冲突，用 relation 和 target_id 标明。没有值得记的就返回空列表。"""


def digest(session: Session) -> str:
    traj: Trajectory = session.trajectory  # type: ignore[assignment]
    calls = "\n".join(
        f"- {c['name']}({json.dumps(c['input'], ensure_ascii=False)}) → {c['status']}: {c['result'][:500]}"
        for c in traj.tool_calls)
    return (f"## 工单\n{json.dumps(session.ticket, ensure_ascii=False)}\n\n## 工具调用\n{calls}\n\n"
            f"## 最终回复\n{traj.reply[:1500]}\n\n## 质检意见\n{traj.feedback or '无'}")


def extract(session: Session, store: MemoryStore) -> list[Candidate]:
    related = store.relevant(session.ticket, limit=10)
    existing = "\n".join(f"- {m.id}（{KIND_LABEL[m.kind]} · {m.subject}）{m.content}" for m in related) or "（无）"
    extractor = Agent(model=MODEL_FACTORY(), system_prompt=EXTRACT_PROMPT, callback_handler=None)
    prompt = f"{digest(session)}\n\n## 已有的相关记忆\n{existing}"
    result = extractor(prompt, structured_output_model=Extraction)
    return result.structured_output.items


# ---- 治理 --------------------------------------------------------------------------------

def govern(store: MemoryStore, candidates: list[Candidate], source: str) -> list[dict[str, Any]]:
    """Apply admission, dedupe, disambiguation; returns what happened to each candidate."""
    log = []
    for c in candidates:
        if c.confidence < MIN_CONFIDENCE:
            log.append({"action": "rejected", "reason": "low confidence", "content": c.content})
            continue
        target = store.get(c.target_id) if c.relation != "new" else None
        if target is None or not target.active:
            target = store.find_duplicate(c.kind, c.subject, c.content)  # model missed a duplicate
            relation = "duplicate" if target else "new"
        else:
            relation = c.relation
        if relation == "duplicate":
            target.sources = list(dict.fromkeys([*target.sources, source]))
            target.keywords = list(dict.fromkeys([*target.keywords, *c.keywords]))
            target.confidence = min(1.0, round(target.confidence + 0.1, 2))
            log.append({"action": "merged", "into": target.id, "content": c.content})
        elif relation in ("update", "contradicts"):
            new = store.add(c.kind, c.subject, c.content, source, c.confidence, c.keywords)
            target.status, target.note = "superseded", f"（被 {new.id} 替代：{relation}）"
            log.append({"action": "superseded", "old": target.id, "new": new.id, "content": c.content})
        else:
            new = store.add(c.kind, c.subject, c.content, source, c.confidence, c.keywords)
            log.append({"action": "added", "id": new.id, "content": c.content})
    return log


def end_session(session: Session) -> None:
    """on_session_end: 采集 → 抽取 → 治理."""
    store = MemoryStore(session.memory_dir)
    store.sessions += 1
    # 采集
    raw = session.memory_dir / "trajectories"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"{session.ticket['id']}.json").write_text(
        json.dumps(session.trajectory.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    # 抽取 + 治理
    log = govern(store, extract(session, store), session.ticket["id"])
    forgotten = store.forget_stale()
    store.save()
    trace.event("memory_update", ticket=session.ticket["id"], changes=log,
                forgotten=[m.id for m in forgotten], active=sum(m.active for m in store.items))


# ---- 使用 --------------------------------------------------------------------------------

def memory_prompt(session: Session) -> str:
    """Relevant memories for this session, as a system-prompt section (and count their use)."""
    store = MemoryStore(session.memory_dir)
    hits = store.relevant(session.ticket)
    if not hits:
        return ""
    for m in hits:
        m.uses += 1
        m.last_used_session = store.sessions + 1
    store.save()
    trace.event("memory_injected", ticket=(session.ticket or {}).get("id"),
                entries=[{"id": m.id, "from": m.sources, "content": m.content} for m in hits])
    lines = "\n".join(f"- [{m.id} · {KIND_LABEL[m.kind]} · {m.subject}] {m.content}（来源：{'、'.join(m.sources)}）"
                      for m in hits)
    return ("\n\n## 长期记忆（来自之前的工单）\n以下是之前处理工单时积累的经验，与本工单相关时直接使用：\n" + lines)


def make_recall(session: Session) -> Any:
    """Pull-style use: the agent searches memory when it meets something it may have seen before."""
    from strands import tool

    @tool
    def recall(query: str) -> list[dict]:
        """搜索长期记忆（之前工单积累的事实、客户偏好、经验）。遇到不熟悉的系统现象、支付网关、
        或客户描述与系统记录不一致时先查询。query 可以是系统名、客户编号或现象描述。"""
        store = MemoryStore(session.memory_dir)
        probe = {"customer_id": query, "order_id": query, "subject": query, "body": query}
        hits = [m for m in store.relevant(probe) if m.kind != "lesson" or query in m.content]
        trace.event("memory_recall", ticket=(session.ticket or {}).get("id"), query=query,
                    hits=[m.id for m in hits])
        return [{"id": m.id, "subject": m.subject, "content": m.content, "from": m.sources} for m in hits]

    return recall


# ---- 命令 --------------------------------------------------------------------------------

def show_memory(arg: str, ctx: CommandContext) -> None:
    """/memory [all] — active memories (or every entry with 'all')."""
    store = MemoryStore(ctx.memory_dir)
    for m in store.items:
        if m.active or arg == "all":
            flag = "" if m.active else f" [{m.status}{m.note}]"
            print(f"  {m.id} {KIND_LABEL[m.kind]}·{m.subject}: {m.content} (来源 {'、'.join(m.sources)}, "
                  f"置信 {m.confidence:.2f}, 使用 {m.uses}){flag}")
    print(f"共 {sum(m.active for m in store.items)} 条有效记忆，{store.sessions} 个会话。")


def forget_command(arg: str, ctx: CommandContext) -> None:
    """/forget <关键词> — mark matching memories as forgotten (kept in the file for audit)."""
    store = MemoryStore(ctx.memory_dir)
    hits = store.forget(arg)
    store.save()
    trace.event("memory_forget", keyword=arg, ids=[m.id for m in hits])
    print(f"已遗忘 {len(hits)} 条：{[m.id for m in hits]}")
