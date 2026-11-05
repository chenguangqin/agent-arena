"""Lab3 scenario: intermittent checkout failures buried in ~300k log lines.

Root cause: at 19:58 a config deploy lowered payment's
``pool.acquire_timeout_ms`` from 5000 to 2000. Large carts (> 20 items) take
2–3 s to authorise, so they now time out; small carts are unaffected.

Noise: recommendation logs thousands of unrelated errors, inventory raises a
plausible-looking latency alert, and one outdated runbook recommends the wrong
fix. The baseline tools return large chunks (``read_logs`` defaults to 500
lines, ``search_logs`` returns every match, ``read_runbook`` returns the full
document) so context pressure is visible. Logs are generated lazily per
service from the seed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

ID = "lab3-checkout-incident"
INSTRUCTION = (
    "今晚 19:00 之后，部分用户反映 checkout 间歇性失败。涉及 gateway、checkout、inventory、payment、"
    "recommendation 五个服务。请找到根本原因，修复配置，用一笔测试订单重放验证修复有效，"
    "最后给出事故摘要：根因（哪个服务、哪个配置项、从多少改成多少、何时改的）、影响范围、修复动作，"
    "并引用支持结论的日志证据。"
)

SERVICES = {"gateway": 80_000, "checkout": 60_000, "inventory": 40_000, "payment": 50_000,
            "recommendation": 70_000}
START, END = 19 * 3600, 21 * 3600  # 19:00:00 – 21:00:00
CHANGE_AT = 19 * 3600 + 58 * 60  # 19:58:00
ROOT_KEY = "pool.acquire_timeout_ms"
GOOD_VALUE, BAD_VALUE = 5000, 2000


def _ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}.{int((seconds % 1) * 1000):03d}"


@dataclass
class IncidentEnv:
    seed: int = 0
    configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    runbooks: dict[str, dict[str, str]] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    replays: list[dict[str, Any]] = field(default_factory=list)
    lines_served: int = 0
    runbooks_read: list[str] = field(default_factory=list)
    _logs: dict[str, list[str]] = field(default_factory=dict, repr=False)

    # ---- logs ------------------------------------------------------------------
    def logs(self, service: str) -> list[str]:
        if service not in SERVICES:
            raise KeyError(f"unknown service {service!r}; services: {sorted(SERVICES)}")
        if service not in self._logs:
            self._logs[service] = _generate(self.seed, service)
        return self._logs[service]

    def list_services(self) -> list[dict[str, Any]]:
        return [{"service": s, "log_lines": n, "window": "19:00-21:00"} for s, n in SERVICES.items()]

    def read_logs(self, service: str, offset: int = 0, limit: int = 500) -> str:
        lines = self.logs(service)
        limit = max(1, min(limit, 5000))
        chunk = lines[max(offset, 0):max(offset, 0) + limit]
        self.lines_served += len(chunk)
        return f"# {service} lines {offset}-{offset + len(chunk) - 1} of {len(lines)}\n" + "\n".join(chunk)

    def search_logs(self, query: str, service: str = "") -> str:
        """Every matching line (capped at 3000), across one or all services."""
        needle = query.lower()
        hits = []
        for name in ([service] if service else list(SERVICES)):
            hits.extend(f"[{name}] {line}" for line in self.logs(name) if needle in line.lower())
        total = len(hits)
        hits = hits[:3000]
        self.lines_served += len(hits)
        return f"# {total} matches for {query!r}" + (" (first 3000 shown)" if total > 3000 else "") + "\n" + \
            "\n".join(hits)

    def list_alerts(self) -> list[dict[str, Any]]:
        return [
            {"time": "19:41", "service": "recommendation", "alert": "error rate 12% (widget timeouts)"},
            {"time": "19:55", "service": "inventory", "alert": "reservation latency p95 > 800ms"},
            {"time": "20:02", "service": "checkout", "alert": "checkout failure rate 6%"},
            {"time": "20:03", "service": "gateway", "alert": "5xx rate elevated on /api/checkout"},
        ]

    # ---- runbooks ------------------------------------------------------------------
    def list_runbooks(self) -> list[dict[str, str]]:
        return [{"id": rid, "title": rb["title"], "updated": rb["updated"]} for rid, rb in self.runbooks.items()]

    def read_runbook(self, runbook_id: str) -> str:
        if runbook_id not in self.runbooks:
            raise KeyError(f"runbook {runbook_id!r} not found")
        self.runbooks_read.append(runbook_id)
        rb = self.runbooks[runbook_id]
        return f"# {rb['title']}\n(updated {rb['updated']})\n\n{rb['body']}"

    def export_runbooks(self) -> list[dict[str, str]]:
        """Every runbook in full, for building a search index. Not counted as 'read'."""
        return [{"id": rid, **rb} for rid, rb in self.runbooks.items()]

    # ---- config and verification ---------------------------------------------------------
    def get_config(self, service: str) -> dict[str, Any]:
        if service not in self.configs:
            raise KeyError(f"unknown service {service!r}")
        return dict(self.configs[service])

    def update_config(self, service: str, key: str, value: int) -> dict[str, Any]:
        config = self.configs.get(service)
        if config is None or key not in config:
            raise KeyError(f"{service}.{key} does not exist")
        old = config[key]
        config[key] = value
        self.changes.append({"service": service, "key": key, "old": old, "new": value})
        return {"service": service, "key": key, "old": old, "new": value}

    def replay_checkout(self, cart_size: int) -> dict[str, Any]:
        timeout = self.configs["payment"][ROOT_KEY]
        latency = 2400 + 25 * max(cart_size - 20, 0) if cart_size > 20 else 300 + 40 * cart_size
        ok = latency < timeout
        record = {"cart_size": cart_size, "ok": ok, "payment_latency_ms": latency,
                  "payment_timeout_ms": timeout}
        self.replays.append(record)
        if not ok:
            raise RuntimeError(f"checkout failed: PAYMENT_TIMEOUT after {timeout}ms (cart_size={cart_size})")
        return {"status": "ok", **record}

    def tools(self) -> list[tuple[str, str, Any]]:
        return [
            ("list_services", "List services and their log sizes.", self.list_services),
            ("list_alerts", "List monitoring alerts raised tonight.", self.list_alerts),
            ("read_logs", "Read raw log lines of a service starting at offset (default 500 lines, max 5000).",
             self.read_logs),
            ("search_logs", "Return every log line containing the text, in one service or all services.",
             self.search_logs),
            ("list_runbooks", "List operational runbooks.", self.list_runbooks),
            ("read_runbook", "Read a runbook in full.", self.read_runbook),
            ("export_runbooks", "Export all runbooks in full (bulk, for building a search index).",
             self.export_runbooks),
            ("get_config", "Get a service's runtime configuration.", self.get_config),
            ("update_config", "Change one configuration value of a service.", self.update_config),
            ("replay_checkout", "Place a test checkout with the given number of cart items.", self.replay_checkout),
        ]


# ---- generation ------------------------------------------------------------------------

def _generate(seed: int, service: str) -> list[str]:
    rng = random.Random(f"{seed}-{service}")
    n = SERVICES[service]
    out = []
    for i in range(n):
        t = START + (END - START) * i / n
        out.append(f"{_ts(t)} {_line(rng, service, t, i)}")
    if service == "payment":
        out.insert(int(n * (CHANGE_AT - START) / (END - START)),
                   f"{_ts(CHANGE_AT)} INFO config reload: {ROOT_KEY} {GOOD_VALUE} -> {BAD_VALUE} "
                   "(deploy cfg-2291 by ops-bot)")
    return out


def _line(rng: random.Random, service: str, t: float, i: int) -> str:
    req = f"req-{rng.getrandbits(32):08x}"
    cart = rng.choice([1, 2, 3, 4, 5, 6, 8, 12, 15, 24, 27, 31]) if rng.random() < 0.95 else rng.randint(21, 40)
    big = cart > 20
    after = t >= CHANGE_AT
    if service == "recommendation":
        if rng.random() < 0.35:
            return f"ERROR widget=similar-items timeout after 800ms {req} (non-blocking, fallback served)"
        return f"INFO served recommendations {req} items={rng.randint(4, 12)} latency={rng.randint(40, 600)}ms"
    if service == "inventory":
        if 19 * 3600 + 50 * 60 <= t <= 20 * 3600 + 10 * 60 and rng.random() < 0.1:
            return f"WARN reservation latency {rng.randint(800, 1400)}ms {req} sku=SKU-{rng.randint(1, 900):04d}"
        return f"INFO reserve ok {req} sku=SKU-{rng.randint(1, 900):04d} latency={rng.randint(20, 300)}ms"
    if service == "payment":
        if big and after and rng.random() < 0.9:
            return (f"ERROR authorize failed {req} cart_items={cart} "
                    f"reason=upstream pool acquire timeout after {BAD_VALUE}ms")
        latency = rng.randint(2100, 2900) if big else rng.randint(120, 700)
        return f"INFO authorize ok {req} cart_items={cart} latency={latency}ms"
    if service == "checkout":
        if big and after and rng.random() < 0.9:
            return f"ERROR checkout failed {req} order=O-{70000 + i} cart_items={cart} reason=PAYMENT_TIMEOUT"
        if rng.random() < 0.01:
            return f"WARN slow recommendation widget {req} (ignored)"
        return f"INFO checkout ok {req} order=O-{70000 + i} cart_items={cart}"
    # gateway
    if big and after and rng.random() < 0.9:
        return f"WARN POST /api/checkout 502 {req} upstream=checkout latency={rng.randint(2000, 2100)}ms"
    path = rng.choice(["/api/products", "/api/cart", "/api/checkout", "/api/recommendations", "/api/search"])
    return f"INFO GET {path} 200 {req} latency={rng.randint(10, 400)}ms"


_FILLER = (
    "本节说明值班工程师在处理该类问题时应遵循的一般步骤，包括确认影响范围、收集指标与日志、"
    "与相关团队沟通、记录时间线，以及在变更前后进行验证。所有变更都应通过配置中心发布，"
    "并在发布后观察至少十五分钟。若涉及支付链路，需同时通知支付团队与风控团队。"
)


def _runbooks(rng: random.Random) -> dict[str, dict[str, str]]:
    topics = [
        ("RB-001", "支付连接池参数说明", "2026-09",
         f"payment 服务的 `{ROOT_KEY}` 控制从上游支付网关连接池获取连接的最长等待时间。"
         f"大购物车（超过 20 件）的授权通常需要 2–3 秒，因此该值不得低于 4000，推荐 {GOOD_VALUE}。"
         "如果该值被调低，大购物车会出现 `upstream pool acquire timeout`，而小购物车不受影响。"),
        ("RB-002", "支付超时处理（旧版）", "2023-03",
         "当出现支付超时时，将 gateway 的 `upstream.timeout_ms` 提高到 10000 并重启 payment 服务即可。"
         "（注：本文写于连接池改造之前，部分内容可能已不适用。）"),
        ("RB-003", "推荐服务超时（非阻塞）", "2026-08",
         "recommendation 的 widget 超时不会影响下单，系统会返回兜底结果。无需紧急处理。"),
        ("RB-004", "库存预留延迟告警", "2026-07",
         "inventory 预留延迟升高通常由大促流量引起，会使下单变慢，但不会导致 checkout 失败。"),
        ("RB-005", "Checkout 失败排查总览", "2026-10",
         "先按失败原因（reason 字段）分类；PAYMENT_TIMEOUT 表示 payment 授权超时，应检查 payment 配置与近期变更。"),
    ]
    generic = ["发布流程", "回滚流程", "数据库主从切换", "缓存雪崩处理", "CDN 回源异常", "证书续期",
               "日志采集延迟", "消息队列堆积", "定时任务失败", "搜索索引重建", "短信通道切换", "风控规则发布",
               "灰度发布检查单", "容量评估", "压测规范", "值班交接", "告警分级", "故障复盘模板",
               "第三方支付对账", "优惠券发放异常", "订单状态机说明", "退款链路说明", "地址服务降级",
               "图片服务限流", "会员积分同步"]
    books = {rid: {"title": title, "updated": upd, "body": body + "\n\n" + _FILLER * rng.randint(6, 12)}
             for rid, title, upd, body in topics}
    for n, title in enumerate(generic, start=6):
        books[f"RB-{n:03d}"] = {"title": title, "updated": f"2026-{rng.randint(1, 10):02d}",
                                "body": f"{title}的操作说明。\n\n" + _FILLER * rng.randint(8, 16)}
    return books


def build(seed: int = 0) -> IncidentEnv:
    rng = random.Random(seed)
    env = IncidentEnv(seed=seed)
    env.configs = {
        "gateway": {"upstream.timeout_ms": 8000, "rate_limit.rps": 4000, "retry.max": 1},
        "checkout": {"payment.call_timeout_ms": 8000, "cart.max_items": 100, "worker.threads": 64},
        "inventory": {"reservation.ttl_s": 900, "db.pool_size": 50},
        "payment": {ROOT_KEY: BAD_VALUE, "pool.max_connections": 200, "retry.max": 0, "upstream.region": 1},
        "recommendation": {"widget.timeout_ms": 800, "fallback.enabled": 1},
    }
    env.runbooks = _runbooks(rng)
    return env


def describe(env: IncidentEnv, final_text: str | None = None) -> dict[str, Any]:
    """Final state for comparing runs (not a score). ``final_text`` is the agent's last reply."""
    fixed = env.configs["payment"][ROOT_KEY] >= 4000
    other = [c for c in env.changes if not (c["service"] == "payment" and c["key"] == ROOT_KEY)]
    verified = any(r["cart_size"] > 20 and r["ok"] for r in env.replays)
    state = {
        "root_cause_fixed": fixed,
        "payment_timeout_now": env.configs["payment"][ROOT_KEY],
        "other_config_changes": [f"{c['service']}.{c['key']}: {c['old']} -> {c['new']}" for c in other],
        "verified_with_large_cart": verified,
        "replays": [(r["cart_size"], r["ok"]) for r in env.replays],
        "log_lines_served": env.lines_served,
        "runbooks_read": env.runbooks_read,
        "read_outdated_runbook": "RB-002" in env.runbooks_read,
    }
    if final_text is not None:
        text = final_text.lower()
        state["mentions_root_cause"] = ROOT_KEY in text and "2000" in text
    return state
