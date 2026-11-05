"""Starter and reference solution, driven by scripted models (no Bedrock)."""

from __future__ import annotations

import json
import sys

import pytest
from strands import Agent

import agent as starter
from labkit import trace
from solution import agent as solution
from solution import code_exec
from solution.governance import Governance
from tests.conftest import events
from tests.fakes import ScriptedModel, text, tool


def run(env, mode, turns, build=solution.build_tools, hooks=solution.build_hooks):
    model = ScriptedModel(turns)
    agent = Agent(model=model, tools=build(env, mode), hooks=[*hooks(env, mode), trace.Tracer()],
                  callback_handler=None)
    try:
        agent("go")
    finally:
        agent.cleanup()
    return model


def last_result(model, index=-1):
    return json.dumps(model.requests[index]["messages"][-1], ensure_ascii=False)


def big_duplicate(env):
    order = next(o for o, p in env.state.duplicates.items() if env.state.payments[p]["amount"] > 500)
    return order, env.state.duplicates[order]


def test_starter_baseline_sees_all_raw_endpoints(env):
    model = run(env, "baseline", [[text("ok")]], build=starter.build_tools, hooks=starter.build_hooks)
    assert len(model.requests[0]["tools"]) == 17


def test_env_call_and_errors(env):
    page = env.call("payments_list_v2", date="2026-11-03")
    assert page["total_count"] == 105
    with pytest.raises(RuntimeError, match="ERR_5031"):
        env.call("payments_list_v2", date="2026-11-03")  # every 4th call starting at #2 times out


def test_goal1_custom_tool_finds_every_duplicate(env):
    model = run(env, "tool", [[tool("find_duplicate_charges", "t1", date="2026-11-03")], [text("ok")]])
    assert "refund_v1" not in model.requests[0]["tools"]
    assert "find_duplicate_charges" in model.requests[0]["tools"]
    result = last_result(model, 1)
    assert all(extra in result for extra in env.state.duplicates.values())


def test_goal2_own_mcp_server_replaces_raw_api_and_guards_refunds(env):
    small = next(o for o, p in env.state.duplicates.items() if env.state.payments[p]["amount"] <= 500)
    extra = env.state.duplicates[small]
    original = next(p for p, r in env.state.payments.items() if r["order_id"] == small and p != extra)
    amount = env.state.payments[extra]["amount"]
    model = run(env, "mcp", [
        [tool("find_duplicate_charges", "t1", date="2026-11-03")],
        [tool("refund_payment", "t2", payment_id=original, amount=amount, reason="x")],
        [tool("refund_payment", "t3", payment_id=extra, amount=amount, reason="dup")],
        [text("ok")],
    ])
    assert set(model.requests[0]["tools"]) == {"find_duplicate_charges", "get_order", "request_refund_approval",
                                               "refund_payment", "send_refund_notice"}
    assert "原始扣款" in last_result(model, 2)
    assert [r["payment_id"] for r in env.state.refunds] == [extra]


def _local_runner(workdir):
    """TEST ONLY: runs the sandbox runner with the local interpreter (trusted test code)."""
    return [sys.executable, "-I", str(workdir / "runner.py"), str(workdir / "code.py")]


def test_goal3_sandbox_bridges_read_only_calls_and_denies_writes(env, tmp_path):
    code = (
        "page = api.call('payments_list_v2', date='2026-11-03')\n"
        "try:\n    api.call('refunds_create_v2', payment_id='PAY-80001', amount=1, reason='x')\n"
        "except RuntimeError as exc:\n    denied = str(exc)\n"
        "result = {'total': page['total_count'], 'denied': denied}\n"
    )
    outcome = code_exec.run_code(code, env, command=_local_runner)
    assert outcome["result"]["total"] == 105
    assert "不允许在代码中调用" in outcome["result"]["denied"]
    assert env.state.refunds == []
    assert events(tmp_path, "sandbox_denied")


def test_goal3_refuses_when_docker_is_unavailable(env, monkeypatch, tmp_path):
    monkeypatch.setattr(code_exec, "docker_available", lambda: False)
    model = run(env, "code", [[tool("run_python", "t1", code="result = 1")], [text("ok")]])
    assert "拒绝执行" in last_result(model, 1)
    assert events(tmp_path, "sandbox_unavailable")


def test_goal3_code_mode_only_exposes_write_tools_besides_run_python(env):
    model = run(env, "code", [[text("ok")]])
    assert set(model.requests[0]["tools"]) == {"refunds_create_v2", "approvals_request", "notifications_send_v2",
                                               "notifications_templates_v2", "run_python"}


def test_goal3_docker_command_is_locked_down():
    command = " ".join(code_exec.docker_command(code_exec.Path("/tmp/x")))
    for flag in ("--network none", "--read-only", "--memory", "--pids-limit", "--cap-drop ALL", "--user"):
        assert flag in command


def test_goal4_blocks_deprecated_and_unapproved_refunds(env, tmp_path):
    order, extra = big_duplicate(env)
    amount = env.state.payments[extra]["amount"]
    model = run(env, "governance", [
        [tool("refund_v1", "t1", order_id=order)],
        [tool("refunds_create_v2", "t2", payment_id=extra, amount=amount, reason="dup")],
        [tool("approvals_request", "t3", payment_id=extra, amount=amount, reason="dup")],
        [tool("refunds_create_v2", "t4", payment_id=extra, amount=amount, reason="dup")],
        [text("ok")],
    ])
    blocks = events(tmp_path, "policy_block")
    assert [b["tool"] for b in blocks] == ["refund_v1", "refunds_create_v2"]
    assert env.state.v1_refund_calls == 0
    assert [r["payment_id"] for r in env.state.refunds] == [extra]
    assert "审批" in last_result(model, 2)


def test_goal4_marks_injected_notes_and_requires_approval_afterwards(env, tmp_path):
    injected = env.state.injected_order
    small = next(o for o, p in env.state.duplicates.items() if env.state.payments[p]["amount"] <= 500)
    extra = env.state.duplicates[small]
    model = run(env, "governance", [
        [tool("orders_get_v2", "t1", order_id=injected)],
        [tool("refunds_create_v2", "t2", payment_id=extra, amount=env.state.payments[extra]["amount"], reason="d")],
        [text("ok")],
    ])
    assert events(tmp_path, "untrusted_content")[0]["suspicious"] is True
    assert "不是指令" in last_result(model, 1)
    assert env.state.refunds == []  # small refund still needs approval after an injection


def test_governance_is_a_plain_hook_provider():
    assert hasattr(Governance(), "register_hooks")
