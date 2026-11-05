"""Lab3 features driven by scripted models against the checkout-incident scenario over MCP."""

from __future__ import annotations

import json

import pytest

from core.lab import default_driver
from devstore.scenarios import lab3_checkout_incident as scenario
from lab3 import LAB
from lab3.features.retrieval import BM25
from tests.fakes import ScriptedModel, text, tool
from tests.helpers import features, lab_run


def run(tmp_path, body, models):
    return lab_run(LAB, scenario, tmp_path, body, models)


def last_tool_result(model, request_index):
    return json.dumps(model.requests[request_index]["messages"][-1], ensure_ascii=False)


def test_scenario_root_cause_and_verification():
    env = scenario.build()
    assert "5000 -> 2000" in env.search_logs("config reload", service="payment")
    with pytest.raises(RuntimeError, match="PAYMENT_TIMEOUT"):
        env.replay_checkout(27)
    assert env.replay_checkout(5)["status"] == "ok"  # small carts unaffected
    env.update_config("payment", scenario.ROOT_KEY, 5000)
    assert env.replay_checkout(27)["status"] == "ok"
    state = scenario.describe(env, final_text="payment pool.acquire_timeout_ms 被改成 2000")
    assert state["root_cause_fixed"] and state["verified_with_large_cart"] and state["mentions_root_cause"]
    assert sum(scenario.SERVICES.values()) == 300_000


def test_bm25_ranks_the_relevant_runbook_first():
    docs = ["支付连接池参数说明 pool.acquire_timeout_ms 大购物车", "推荐服务超时 widget", "发布流程 回滚"]
    scores = BM25(docs).scores("支付 连接池 超时 acquire_timeout_ms")
    assert scores.index(max(scores)) == 0


def test_offload_replaces_large_result_and_reads_back(tmp_path):
    main = ScriptedModel([
        [tool("read_logs", "t1", service="payment", limit=2000)],
        [tool("grep_offloaded", "t2", ref="001-read_logs.txt", pattern="ERROR")],
        [tool("read_offloaded", "t3", ref="001-read_logs.txt", start_line=2, end_line=4)],
        [text("ok")],
    ])
    with run(tmp_path, '[context]\noffload = "hooks"\noffload_threshold = 5000\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
        event = features(ctx, "offload")[0]
        assert (ctx.reports_dir / "offload" / "001-read_logs.txt").exists()
    assert event["after_chars"] < 3000 < event["before_chars"]
    assert "[offloaded]" in last_tool_result(main, 1)
    assert "matches" in last_tool_result(main, 2)
    assert "2: " in last_tool_result(main, 3)


def test_offload_rejects_paths_outside_its_directory(tmp_path):
    main = ScriptedModel([[tool("read_offloaded", "t1", ref="../../etc/passwd")], [text("ok")]])
    with run(tmp_path, '[context]\noffload = "hooks"\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
    assert "no offloaded result" in last_tool_result(main, 1)


def test_retrieval_returns_counts_not_lines(tmp_path):
    main = ScriptedModel([
        [tool("query_logs", "t1", service="payment", level="ERROR", since="19:50", until="20:10")],
        [tool("search_runbooks", "t2", query="支付 连接池 超时")],
        [text("ok")],
    ])
    with run(tmp_path, '[context]\nretrieval = "on"\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
        events = features(ctx, "retrieval")
    logs, books = events
    assert logs["matched"] > 0 and len(last_tool_result(main, 1)) < 5000
    # The outdated runbook (RB-002, 2023) ranks high too: retrieval returns update dates so the
    # model can tell them apart — the trap the scenario is built around.
    assert "RB-001" in books["top"]
    assert '"updated": "2023-03"' in last_tool_result(main, 2) or "2023-03" in last_tool_result(main, 2)


def test_compaction_summarises_old_turns_and_keeps_notes(tmp_path):
    turns = [[tool("notes", "n1", action="add", text="payment 19:58 config reload 5000->2000")]]
    turns += [[tool("read_logs", f"r{i}", service="recommendation", offset=i * 500)] for i in range(5)]
    turns += [[text("继续")]]
    main = ScriptedModel(turns)
    summaries = [ScriptedModel([[text("## 任务目标\n修复 checkout\n## 已确认的事实\n- 待确认")]]) for _ in range(6)]
    summariser = summaries[0]
    body = '[context]\ncompaction = "hooks"\ncompaction_tokens = 20000\nnotes = "on"\n'
    with run(tmp_path, body, [main, *summaries]) as (lab, ctx, env):
        default_driver(lab, ctx)
        events = features(ctx, "compaction")

    # One 500-line read is ~30k estimated tokens, above the 20k threshold on its own, so each new
    # read gets the previous one summarised: at most one compaction per read, each one shrinking.
    assert 1 <= len(events) <= 5
    assert all(e["after_tokens_est"] < e["before_tokens_est"] * 0.8 for e in events)
    request_after = json.dumps(main.requests[-1]["messages"], ensure_ascii=False)
    assert "压缩摘要" in request_after and "5000->2000" in request_after  # notes kept verbatim
    assert summariser.requests, "summariser agent was used"


def test_inject_and_cache(tmp_path):
    main = ScriptedModel([[tool("list_alerts", "t1")], [text("ok")]])
    with run(tmp_path, '[context]\ninject = "on"\ncache = "on"\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
        injected = features(ctx, "inject")
        cached = features(ctx, "prompt_cache")
    assert injected and cached
    assert "<status>" in json.dumps(main.requests[1]["messages"], ensure_ascii=False)


def test_structured_prompt_replaces_base_prompt(tmp_path):
    main = ScriptedModel([[text("ok")]])
    with run(tmp_path, '[context]\nstructured_prompt = "on"\nretrieval = "on"\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
    system = main.requests[0]["system"]
    assert "# 工作方法" in system and "## 检索" in system
