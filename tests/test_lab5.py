"""Lab5 features driven by scripted models against the real scenario over MCP."""

from __future__ import annotations

import json

import pytest

from core.lab import default_driver
from devstore.scenarios import lab5_promo_incident as scenario
from lab5 import LAB
from tests.fakes import ScriptedModel, text, tool
from tests.helpers import features, lab_run


def test_scenario_dynamics():
    env = scenario.build()
    order = next(o for o in env.orders.values() if o["failed_at"] and not o["_sold_out"])
    with pytest.raises(RuntimeError, match="ERR_NEGATIVE_TOTAL"):
        env.replay_order(order["id"])  # rule not fixed yet
    env.update_discount_rule(scenario.BAD_RULE, 50)
    outcomes = []
    for _ in range(12):
        try:
            env.replay_order(order["id"])
            outcomes.append("ok")
            break
        except RuntimeError as exc:
            outcomes.append("503" if "503" in str(exc) else str(exc))
    assert outcomes[-1] == "ok"
    sold = next(o for o in env.orders.values() if o["_sold_out"])
    env.replay_calls = 100  # past the outage
    with pytest.raises(RuntimeError, match="OUT_OF_STOCK"):
        env.replay_order(sold["id"])
    env.issue_refund(sold["payment_id"], sold["amount"], "sold out")
    assert env.orders[sold["id"]]["status"] == "refunded"
    with pytest.raises(ValueError):
        env.issue_refund(order["payment_id"], 1.0, "already replayed")
    customers = {o["customer_id"] for o in env.orders.values() if o["failed_at"]}
    failed = [o for o in env.orders.values() if o["failed_at"]]
    assert len(failed) == 40 and len(customers) == 30  # repeat customers must be notified once


def lab5_run(tmp_path, profile_body, models):
    return lab_run(LAB, scenario, tmp_path, profile_body, models)


def test_gate_requires_finish_then_runs_checks_and_gives_up_at_round_limit(tmp_path):
    main = ScriptedModel([
        [text("都处理完了。")],  # stops without finish → resume
        [tool("finish", "f1", summary="done", evidence="none", unresolved="无")],  # checks fail → resume
        [tool("finish", "f2", summary="done", evidence="none", unresolved="无")],  # round limit → give up
    ])
    body = '[completion]\nfinish_tool = "on"\nchecks = "on"\nmax_rounds = 2\n'
    with lab5_run(tmp_path, body, [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
        gates = features(ctx, "gate")
    assert [g["layer"] for g in gates] == ["finish", "checks", "checks"]
    assert [g["action"] for g in gates][:2] == ["resume", "resume"]
    assert gates[-1]["action"].startswith("give up")
    assert any("折扣规则" in m for m in gates[1]["missing"])
    assert any("复盘报告" in m for m in gates[1]["missing"])
    # The resume message reached the model as a new user turn.
    assert "completion gate" in json.dumps(main.requests[1]["messages"], ensure_ascii=False)


def test_judge_sees_evidence_not_transcript(tmp_path):
    main = ScriptedModel([
        [tool("search_logs", "s1", query="SECRET-REASONING-MARKER")],
        [tool("finish", "f1", summary="修复完成", evidence="规则已改", unresolved="无")],
        [tool("finish", "f2", summary="补充", evidence="已通知", unresolved="无")],
    ])
    judge_fail = ScriptedModel([[tool("Verdict", "v1", passed=False, missing=["没有通知客户的证据"])]])
    judge_pass = ScriptedModel([[tool("Verdict", "v2", passed=True, missing=[])]])
    body = '[completion]\nfinish_tool = "on"\njudge = "hooks"\n'
    with lab5_run(tmp_path, body, [main, judge_fail, judge_pass]) as (lab, ctx, env):
        default_driver(lab, ctx)
        gates = features(ctx, "gate")
    assert [g["layer"] for g in gates] == ["judge", "all"]
    assert gates[0]["missing"] == ["没有通知客户的证据"]
    judge_input = json.dumps(judge_fail.requests[0]["messages"], ensure_ascii=False)
    assert "规则已改" in judge_input and "SECRET-REASONING-MARKER" not in judge_input


def test_todo_is_injected_every_turn_and_nudges_after_repeated_errors(tmp_path):
    main = ScriptedModel([
        [tool("todo_write", "t1", items=[{"content": "修复规则", "status": "in_progress"}])],
        [tool("replay_order", "r1", order_id="O-52000")],
        [tool("replay_order", "r2", order_id="O-52000")],
        [text("暂停")],
    ])
    with lab5_run(tmp_path, '[planning]\ntodo = "on"\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
        nudges = features(ctx, "replan_nudge")
    second_call = json.dumps(main.requests[1]["messages"], ensure_ascii=False)
    assert "<current_plan>" in second_call and "修复规则" in second_call
    assert len(nudges) == 1 and nudges[0]["tool"] == "replay_order"
    assert "todo_write" in json.dumps(main.requests[3]["messages"], ensure_ascii=False)


def test_plan_first_seeds_todo_and_prefaces_task(tmp_path):
    planner = ScriptedModel([[tool("Plan", "p1", steps=[
        {"content": "定位规则", "done_when": "找到规则"}, {"content": "修复", "done_when": "配置正确"}])]])
    main = ScriptedModel([[text("ok")]])
    body = '[planning]\ntodo = "on"\nplan_first = "on"\n'
    with lab5_run(tmp_path, body, [main, planner]) as (lab, ctx, env):
        agent_result = default_driver(lab, ctx)
        assert agent_result is not None
        plan = features(ctx, "plan")
    assert plan[0]["steps"] == 2
    first = json.dumps(main.requests[0]["messages"], ensure_ascii=False)
    assert "定位规则" in first and "已为本任务制定计划" in first


def test_fanout_runs_workers_and_returns_compact_rows(tmp_path):
    main = ScriptedModel([
        [tool("process_orders", "p1", order_ids=["O-52000", "O-52001", "O-52002"])],
        [text("完成")],
    ])
    worker_rows = "\n".join(json.dumps({"order_id": f"O-5200{i}", "outcome": "completed"}) for i in range(2))
    w1 = ScriptedModel([[text(worker_rows)]])
    w2 = ScriptedModel([[text('{"order_id": "O-52002", "outcome": "refunded"}')]])
    body = '[orchestration]\nfanout = "on"\nbatch_size = 2\nmax_workers = 1\n'
    with lab5_run(tmp_path, body, [main, w1, w2]) as (lab, ctx, env):
        default_driver(lab, ctx)
        fan = features(ctx, "fanout")
        tools_seen_by_worker = w1.requests[0]["tools"]
    assert fan[0]["batches"] == 2
    assert sorted(tools_seen_by_worker) == ["get_order", "issue_refund", "replay_order"]
    tool_result = json.dumps(main.requests[1]["messages"][-1], ensure_ascii=False)
    assert "O-52002" in tool_result and "refunded" in tool_result


def test_stall_guard_escalates_and_stops(tmp_path):
    same = [tool("list_alerts", f"a{i}") for i in range(8)]
    main = ScriptedModel([[call] for call in same] + [[text("进度汇报")]])
    with lab5_run(tmp_path, '[guard]\nstall = "hooks"\nthreshold = 3\n', [main]) as (lab, ctx, env):
        result = default_driver(lab, ctx)
        stalls = features(ctx, "stall")
    assert [s["level"] for s in stalls] == [1, 3, 5]
    assert str(result.stop_reason) == "tool_use"  # loop stopped by the guard, not by the model


class _Truncating(ScriptedModel):
    """First response ends with stopReason max_tokens; later turns are scripted."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if not self.requests:
            self.requests.append({"messages": list(messages), "tools": [], "system": system_prompt})
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "通知客户 C3000……"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "max_tokens"}}
            return
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


def test_output_recovery_continues_after_max_tokens(tmp_path):
    from strands.types.exceptions import MaxTokensReachedException

    from lab5.features.recovery import driver

    body = '[guard]\noutput_recovery = "on"\n'
    with lab5_run(tmp_path, body, [_Truncating([[text("分批继续完成")]])]) as (lab, ctx, env):
        result = driver(lab, ctx)
        recoveries = features(ctx, "output_recovery")
    assert len(recoveries) == 1 and "分批继续完成" in str(result)

    with lab5_run(tmp_path / "off", "", [_Truncating([])]) as (lab, ctx, env):
        with pytest.raises(MaxTokensReachedException):
            driver(lab, ctx)
