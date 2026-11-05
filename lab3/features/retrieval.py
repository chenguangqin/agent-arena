"""L3.3 · Retrieve instead of reading.

Two tools that return small, targeted results:

* ``query_logs`` filters one service's logs by level, time window, and
  substring, and returns counts plus a few samples rather than every line;
* ``search_runbooks`` ranks runbook paragraphs with BM25 (implemented here in
  plain Python) and returns only the best passages with their update date.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from strands import tool

from core.lab import AgentSpec, RunContext

GUIDE = """
## 检索
- 查日志优先用 query_logs（按服务、级别、时间窗、关键词过滤，返回计数和少量样例），不要整段读取日志。
- 查 runbook 用 search_runbooks，只看最相关的段落，并注意文档的更新日期。
"""


def tokenize(text: str) -> list[str]:
    """ASCII words plus Chinese character bigrams — enough for BM25 over short docs."""
    words = re.findall(r"[a-z0-9_.]+", text.lower())
    han = re.findall(r"[一-鿿]+", text)
    bigrams = [run[i:i + 2] for run in han for i in range(max(len(run) - 1, 1))]
    return words + bigrams


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = [tokenize(d) for d in docs]
        self.k1, self.b = k1, b
        self.avg = sum(map(len, self.docs)) / max(len(self.docs), 1)
        df = Counter(term for doc in self.docs for term in set(doc))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(doc) for doc in self.docs]

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query)
        out = []
        for doc, tf in zip(self.docs, self.tf, strict=True):
            norm = self.k1 * (1 - self.b + self.b * len(doc) / self.avg)
            out.append(sum(self.idf.get(t, 0) * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in terms if t in tf))
        return out


def _parse_time(value: str) -> str:
    return value.strip()[:8] if value else ""


def install(spec: AgentSpec, ctx: RunContext) -> None:
    passages: list[dict[str, Any]] = []
    index: BM25 | None = None

    def build_index() -> BM25:
        nonlocal index
        if index is None:
            for book in ctx.call_env("list_runbooks"):
                body = ctx.call_env("read_runbook", runbook_id=book["id"])
                for para in [p for p in body.split("\n\n") if p.strip()][:6]:
                    passages.append({"id": book["id"], "title": book["title"], "updated": book["updated"],
                                     "text": para.strip()})
            index = BM25([f"{p['title']} {p['text']}" for p in passages])
        return index

    @tool
    def query_logs(service: str, contains: str = "", level: str = "", since: str = "", until: str = "",
                   samples: int = 5) -> dict:
        """Filter one service's logs. level: INFO|WARN|ERROR; since/until: HH:MM[:SS]; contains: substring.
        Returns the match count, counts per minute (first and last 10), and a few sample lines."""
        result = ctx.call_env("search_logs", query=contains or (level or " "), service=service)
        header, *rows = result.split("\n")
        lines = [line.split("] ", 1)[1] for line in rows if "] " in line]
        truncated = "first 3000 shown" in header
        start, end = _parse_time(since), _parse_time(until)
        matched = [line for line in lines
                   if (not level or f" {level.upper()} " in f" {line} ")
                   and (not start or line[:len(start)] >= start) and (not end or line[:len(end)] <= end)]
        per_minute = Counter(line[:5] for line in matched)
        minutes = sorted(per_minute)
        ctx.trace.feature("retrieval", tool="query_logs", service=service, scanned=len(lines), matched=len(matched))
        return {
            "service": service, "matched": len(matched),
            "note": "only the first 3000 raw matches were scanned; narrow the filter" if truncated else "",
            "first_minutes": {m: per_minute[m] for m in minutes[:10]},
            "last_minutes": {m: per_minute[m] for m in minutes[-10:]},
            "samples": matched[: max(1, min(samples, 20))],
        }

    @tool
    def search_runbooks(query: str, k: int = 3) -> list[dict]:
        """Top-k runbook passages for a query (BM25), each with runbook id, title and update date."""
        scores = build_index().scores(query)
        ranked = sorted(range(len(passages)), key=lambda i: -scores[i])[: max(1, min(k, 8))]
        ctx.trace.feature("retrieval", tool="search_runbooks", query=query, passages=len(ranked),
                          top=[passages[i]["id"] for i in ranked])
        return [{**passages[i], "text": passages[i]["text"][:600], "score": round(scores[i], 2)} for i in ranked]

    spec.tools.extend([query_logs, search_runbooks])
    spec.system_prompt += GUIDE
