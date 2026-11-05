"""目标 4 · 按需检索，替代整段读取。

* query_logs：按服务、级别、时间窗、关键词过滤日志，只返回匹配数、按分钟的分布和少量样例；
* search_runbooks：用 BM25（纯 Python 实现）检索 runbook 段落，返回最相关的几段，并附更新日期，
  方便模型识别过时的文档。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from strands import tool

from labkit import Env, trace

# Tools that pull whole logs or documents into the context; hidden in retrieval mode.
BULK_TOOLS = ["read_logs", "search_logs", "read_runbook", "export_runbooks"]


def tokenize(text: str) -> list[str]:
    """ASCII words plus Chinese character bigrams — enough for BM25 over short documents."""
    words = re.findall(r"[a-z0-9_.]+", text.lower())
    han = re.findall(r"[一-鿿]+", text)
    return words + [run[i:i + 2] for run in han for i in range(max(len(run) - 1, 1))]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = [tokenize(d) for d in docs]
        self.k1, self.b = k1, b
        self.avg = sum(map(len, self.docs)) / max(len(self.docs), 1)
        df = Counter(term for doc in self.docs for term in set(doc))
        self.idf = {t: math.log(1 + (len(self.docs) - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(doc) for doc in self.docs]

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query)
        out = []
        for doc, tf in zip(self.docs, self.tf, strict=True):
            norm = self.k1 * (1 - self.b + self.b * len(doc) / self.avg)
            out.append(sum(self.idf.get(t, 0) * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in terms if t in tf))
        return out


def make_retrieval_tools(env: Env) -> list[Any]:
    passages: list[dict[str, str]] = []
    index: list[BM25] = []

    def build_index() -> BM25:
        if not index:
            for book in env.call("export_runbooks"):
                for para in [p.strip() for p in book["body"].split("\n\n") if p.strip()][:6]:
                    passages.append({"id": book["id"], "title": book["title"], "updated": book["updated"],
                                     "text": para})
            index.append(BM25([f"{p['title']} {p['text']}" for p in passages]))
        return index[0]

    @tool
    def query_logs(service: str, contains: str = "", level: str = "", since: str = "", until: str = "",
                   samples: int = 5) -> dict:
        """过滤一个服务的日志。level: INFO|WARN|ERROR；since/until: HH:MM[:SS]；contains: 子串。
        返回匹配行数、按分钟的计数（前 10 分钟和后 10 分钟）以及少量样例行，不返回全部日志。"""
        raw = env.call("search_logs", query=contains or level or " ", service=service)
        header, *rows = raw.split("\n")
        lines = [row.split("] ", 1)[1] for row in rows if "] " in row]
        matched = [line for line in lines
                   if (not level or f" {level.upper()} " in f" {line} ")
                   and (not since or line[:len(since)] >= since) and (not until or line[:len(until)] <= until)]
        per_minute = Counter(line[:5] for line in matched)
        minutes = sorted(per_minute)
        trace.event("retrieval", tool="query_logs", service=service, matched=len(matched))
        return {"service": service, "matched": len(matched),
                "note": "原始匹配超过 3000 行，只扫描了前 3000 行，请缩小条件" if "first 3000" in header else "",
                "first_minutes": {m: per_minute[m] for m in minutes[:10]},
                "last_minutes": {m: per_minute[m] for m in minutes[-10:]},
                "samples": matched[: max(1, min(samples, 20))]}

    @tool
    def search_runbooks(query: str, k: int = 3) -> list[dict]:
        """检索 runbook，返回最相关的 k 个段落（BM25），每段带 runbook 编号、标题和更新日期。"""
        scores = build_index().scores(query)
        ranked = sorted(range(len(passages)), key=lambda i: -scores[i])[: max(1, min(k, 8))]
        trace.event("retrieval", tool="search_runbooks", query=query, top=[passages[i]["id"] for i in ranked])
        return [{**passages[i], "text": passages[i]["text"][:600], "score": round(scores[i], 2)} for i in ranked]

    return [query_logs, search_runbooks]
