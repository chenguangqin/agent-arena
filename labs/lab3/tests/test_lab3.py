"""Starter and reference solution, driven by scripted models (no Bedrock)."""

from __future__ import annotations

import json

import pytest
from strands import Agent

import agent as starter
from labkit import trace
from labkit.cli import dispatch
from solution import agent as solution
from solution.commands import context_breakdown, handle_command
from solution.compaction import Compactor
from solution.offload import Offload, make_offload_readers
from tests.conftest import events
from tests.fakes import ScriptedModel, text, tool


def make_agent(env, mode, turns, **kwargs):
    model = ScriptedModel(turns)
    agent = Agent(model=trace.tap(model), tools=solution.build_tools(env, mode), system_prompt=solution.SYSTEM_PROMPT,
                  hooks=[*solution.build_hooks(env, mode), trace.Tracer()], callback_handler=None, **kwargs)
    return agent, model


def summarizer(summary="## 任务目标\n修 checkout\n## 已排除的假设\nrecommendation 无关"):
    calls = []

    def factory():
        model = ScriptedModel([[text(summary)]])
        calls.append(model)
        return Agent(model=model, callback_handler=None)

    factory.calls = calls
    return factory


def test_starter_has_no_commands_and_chat_reports_unknown(env, capsys):
    agent = Agent(model=ScriptedModel([]), callback_handler=None)
    assert starter.handle_command(agent, "/context") is False
    assert dispatch(agent, env, "/context", starter.handle_command) is None
    assert "unknown command" in capsys.readouterr().out
    assert dispatch(agent, env, "/reset", starter.handle_command) == "reset"


def test_goal1_context_breakdown_tracks_tool_results(env, capsys):
    agent, model = make_agent(env, "baseline", [[tool("search_logs", "t1", query="ERROR", service="payment")],
                                                [text("看到了")]])
    agent("查一下 payment 的错误")
    parts = context_breakdown(agent)
    assert parts["tool_results"] > 10 * parts["system_prompt"]
    assert handle_command(agent, "/context") is True
    out = capsys.readouterr().out
    assert "tool_results" in out and "last model call actually sent" in out
    assert handle_command(agent, "/nope") is False
    agent.cleanup()


def test_goal2_manual_compact_keeps_tool_pairs_and_alternation(env):
    turns = [[tool("list_alerts", f"a{i}")] for i in range(4)] + [[text("先到这里")]]
    agent, _ = make_agent(env, "baseline", turns)
    agent("排查 checkout")
    before = len(agent.messages)
    factory = summarizer()
    assert Compactor(summarizer=factory).compact(agent, focus="保留已排除的假设")
    roles = [m["role"] for m in agent.messages]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))  # strictly alternating
    assert len(agent.messages) < before
    assert "压缩摘要" in json.dumps(agent.messages[0], ensure_ascii=False)
    uses = {b["toolUse"]["toolUseId"] for m in agent.messages for b in m["content"] if "toolUse" in b}
    results = {b["toolResult"]["toolUseId"] for m in agent.messages for b in m["content"] if "toolResult" in b}
    assert uses == results  # no orphaned tool results
    assert "保留已排除的假设" in json.dumps(factory.calls[0].requests[0]["messages"], ensure_ascii=False)
    agent.cleanup()


def test_goal2_compact_command_and_auto_compaction(env, tmp_path):
    turns = [[tool("search_logs", f"s{i}", query="ERROR", service="checkout")] for i in range(3)] + [[text("ok")]]
    compactor = Compactor(threshold_tokens=20_000, summarizer=summarizer())
    agent, model = make_agent(env, "baseline", turns, conversation_manager=compactor)
    agent("排查")
    assert compactor.compactions >= 1
    assert events(tmp_path, "compaction")[0]["reason"].startswith("auto")
    assert handle_command(agent, "/compact") in (True,)
    agent.cleanup()


def test_goal3_offload_replaces_large_results_and_reads_back(env, tmp_path):
    directory = tmp_path / "offload"
    model = ScriptedModel([
        [tool("search_logs", "t1", query="cart_items=31", service="payment")],
        [tool("grep_offloaded", "t2", path=str(directory / "001-search_logs.txt"), pattern="acquire timeout")],
        [tool("read_offloaded", "t3", path="/etc/passwd")],
        [text("ok")],
    ])
    agent = Agent(model=model, tools=[env.mcp_client(), *make_offload_readers(directory)],
                  hooks=[Offload(directory), trace.Tracer()], callback_handler=None)
    agent("go")
    agent.cleanup()
    first = json.dumps(model.requests[1]["messages"][-1], ensure_ascii=False)
    assert "已卸载" in first and len(first) < 6000
    assert (directory / "001-search_logs.txt").stat().st_size > 8000
    assert "处匹配" in json.dumps(model.requests[2]["messages"][-1], ensure_ascii=False)
    assert "只能读取" in json.dumps(model.requests[3]["messages"][-1], ensure_ascii=False)
    assert events(tmp_path, "offload")


def test_goal4_retrieval_hides_bulk_tools_and_returns_compact_results(env):
    agent, model = make_agent(env, "retrieval", [
        [tool("query_logs", "q1", service="payment", level="ERROR", since="19:50")],
        [tool("search_runbooks", "r1", query="支付 连接池 超时")],
        [text("ok")],
    ])
    agent("go")
    agent.cleanup()
    seen = set(model.requests[0]["tools"])
    assert not seen & {"read_logs", "search_logs", "read_runbook", "export_runbooks"}
    assert {"query_logs", "search_runbooks"} <= seen
    logs = json.dumps(model.requests[1]["messages"][-1], ensure_ascii=False)
    assert "matched" in logs and len(logs) < 6000
    books = json.dumps(model.requests[2]["messages"][-1], ensure_ascii=False)
    assert "RB-001" in books and "updated" in books
    assert env.state.runbooks_read == []  # building the index does not count as reading


@pytest.mark.parametrize("mode", solution.MODES)
def test_every_solution_mode_builds(env, mode):
    agent = solution.build_agent(env, mode)
    assert agent.tool_names
    agent.cleanup()
