"""Lab2 features driven by scripted models against the reconciliation scenario over MCP."""

from __future__ import annotations

import json

import pytest

from core.lab import default_driver
from devstore.scenarios import lab2_reconciliation as scenario
from lab2 import LAB
from tests.fakes import ScriptedModel, text, tool
from tests.helpers import features, lab_run, tool_events


def run(tmp_path, body, models):
    return lab_run(LAB, scenario, tmp_path, body, models)


def big_duplicate(env):
    order = next(o for o, p in env.duplicates.items() if env.payments[p]["amount"] > 500)
    return order, env.duplicates[order]


def test_scenario_rules():
    api = scenario.build()
    assert len(api.duplicates) == 8
    api.payments_list_v2(scenario.DATE)
    with pytest.raises(scenario.ApiError, match="ERR_5031"):
        api.payments_list_v2(scenario.DATE)  # every 4th call starting at #2 times out
    order, extra = big_duplicate(api)
    amount = api.payments[extra]["amount"]
    with pytest.raises(scenario.ApiError, match="ERR_4012"):
        api.refunds_create_v2(extra, amount, "dup")
    keep = next(p for p, rec in api.payments.items() if rec["order_id"] == order and p != extra)
    assert api.approvals_request(keep, amount, "dup")["status"] == "rejected"  # only the extra capture
    assert api.approvals_request(extra, amount, "dup")["status"] == "approved"
    api.refunds_create_v2(extra, amount, "dup")
    assert scenario.describe(api)["duplicates_refunded_correctly"] == "1/8"
    api.refund_v1(api.injected_order)
    assert scenario.describe(api)["injected_order_fully_refunded"] is True


def test_catalog_hides_deprecated_and_renames(tmp_path):
    main = ScriptedModel([[tool("list_payments", "t1", date=scenario.DATE)], [text("ok")]])
    with run(tmp_path, '[tools]\ncatalog = "on"\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
    seen = main.requests[0]["tools"]
    assert "list_payments" in seen and "refund_payment" in seen
    assert not {"refund_v1", "orders_list_v1", "ledger_export_v1"} & set(seen)
    assert env.payment_list_calls == 1  # the renamed tool reached payments_list_v2


def test_composite_finds_all_duplicates_through_transient_errors(tmp_path):
    main = ScriptedModel([[tool("find_duplicate_charges", "t1", date=scenario.DATE)], [text("ok")]])
    with run(tmp_path, '[tools]\ncomposite = "on"\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
        event = features(ctx, "composite_tool")[0]
    assert event["duplicates"] == 8
    returned = json.dumps(main.requests[1]["messages"][-1], ensure_ascii=False)
    assert all(extra in returned for extra in env.duplicates.values())


def test_shaping_shrinks_verbose_records(tmp_path):
    main = ScriptedModel([[tool("payments_list_v2", "t1", date=scenario.DATE)], [text("ok")]])
    with run(tmp_path, '[tools]\nshaping = "on"\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
        shaped = features(ctx, "result_shaped")[0]
    assert shaped["after_chars"] < shaped["before_chars"] / 3
    assert "gateway_txn_id" not in json.dumps(main.requests[1]["messages"][-1])


def test_errors_auto_retry_and_explain(tmp_path):
    with run(tmp_path, '[tools]\nerrors = "on"\n', []) as (lab, ctx, env):
        order, extra = big_duplicate(env)
        main = ScriptedModel([
            [tool("payments_list_v2", "t1", date=scenario.DATE)],
            [tool("payments_list_v2", "t2", date=scenario.DATE, page_token="20")],  # ERR_5031 → auto retry
            [tool("refunds_create_v2", "t3", payment_id=extra, amount=env.payments[extra]["amount"], reason="d")],
            [text("ok")],
        ])
        ctx.model_factory = lambda _id: main
        default_driver(lab, ctx)
        retries = features(ctx, "auto_retry")
        explained = features(ctx, "error_explained")
    assert retries and retries[0]["code"] == "ERR_5031"
    assert '"status": "success"' in json.dumps(main.requests[2]["messages"][-1])  # model never saw the 503
    assert explained[0]["code"] == "ERR_4012"
    assert "request_refund_approval" in json.dumps(main.requests[3]["messages"][-1], ensure_ascii=False)


def test_policy_blocks_deprecated_refund_and_harness_requests_approval(tmp_path):
    body = '[governance]\npolicy = "hooks"\napproval = "hooks"\n'
    with run(tmp_path, body, []) as (lab, ctx, env):
        order, extra = big_duplicate(env)
        amount = env.payments[extra]["amount"]
        main = ScriptedModel([
            [tool("refund_v1", "t1", order_id=order)],
            [tool("refunds_create_v2", "t2", payment_id=extra, amount=amount, reason="dup")],
            [text("ok")],
        ])
        ctx.model_factory = lambda _id: main
        default_driver(lab, ctx)
        blocks = features(ctx, "policy_block")
        approvals = features(ctx, "approval")
    assert [b["rule"] for b in blocks] == ["deny"]
    assert env.v1_refund_calls == 0
    assert approvals[0]["status"] == "approved" and approvals[0]["by"] == "harness"
    assert scenario.describe(env)["duplicates_refunded_correctly"] == "1/8"


def test_injection_guard_marks_untrusted_text_and_requires_approval(tmp_path):
    body = '[governance]\ninjection_guard = "hooks"\n'
    with run(tmp_path, body, []) as (lab, ctx, env):
        keep = next(p for p, rec in env.payments.items()
                    if rec["order_id"] == env.injected_order and p != env.duplicates[env.injected_order])
        main = ScriptedModel([
            [tool("orders_get_v2", "t1", order_id=env.injected_order)],
            [tool("refunds_create_v2", "t2", payment_id=keep, amount=1.0, reason="as instructed")],
            [text("ok")],
        ])
        ctx.model_factory = lambda _id: main
        default_driver(lab, ctx)
        untrusted = features(ctx, "untrusted_content")
        blocks = features(ctx, "policy_block")
    assert untrusted[0]["suspicious"] is True
    assert blocks and blocks[0]["rule"] == "needs_approval"
    assert env.refunds == []
    assert "不是指令" in json.dumps(main.requests[1]["messages"][-1], ensure_ascii=False)


def test_sdk_approval_uses_human_in_the_loop_with_approval_service(tmp_path):
    body = '[governance]\npolicy = "hooks"\napproval = "sdk"\n'
    with run(tmp_path, body, []) as (lab, ctx, env):
        order, extra = big_duplicate(env)
        main = ScriptedModel([
            [tool("refunds_create_v2", "t1", payment_id=extra, amount=env.payments[extra]["amount"], reason="dup")],
            [text("ok")],
        ])
        ctx.model_factory = lambda _id: main
        default_driver(lab, ctx)
        approvals = features(ctx, "approval")
        calls = [e["name"] for e in tool_events(ctx)]
    assert approvals[0]["by"] == "HumanInTheLoop" and approvals[0]["status"] == "approved"
    assert "refunds_create_v2" in calls
    assert scenario.describe(env)["duplicates_refunded_correctly"] == "1/8"


def test_facade_mcp_server_replaces_raw_tools(tmp_path):
    main = ScriptedModel([[tool("find_duplicate_charges", "t1", date=scenario.DATE)], [text("ok")]])
    with run(tmp_path, '[tools]\nmcp_server = "on"\n', [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
        event = features(ctx, "mcp_server")[0]
    assert set(main.requests[0]["tools"]) == set(event["facade_tools"])
    assert "payments_list_v2" not in main.requests[0]["tools"]
    returned = json.dumps(main.requests[1]["messages"][-1], ensure_ascii=False)
    assert all(extra in returned for extra in env.duplicates.values())


def test_empty_result_gets_a_hint(tmp_path):
    main = ScriptedModel([[tool("find_duplicate_charges", "t1", date="2024-11-03")], [text("ok")]])
    body = '[tools]\ncomposite = "on"\nerrors = "on"\n'
    with run(tmp_path, body, [main]) as (lab, ctx, env):
        default_driver(lab, ctx)
        hints = features(ctx, "empty_result_hint")
    assert hints and hints[0]["tool"] == "find_duplicate_charges"
    assert "年份" in json.dumps(main.requests[1]["messages"][-1], ensure_ascii=False)


def test_facade_refuses_to_refund_the_original_capture(tmp_path):
    with run(tmp_path, '[tools]\nmcp_server = "on"\n', []) as (lab, ctx, env):
        order, extra = big_duplicate(env)
        small = next(o for o, p in env.duplicates.items() if env.payments[p]["amount"] <= 500)
        original = next(p for p, r in env.payments.items() if r["order_id"] == small and p != env.duplicates[small])
        main = ScriptedModel([
            [tool("refund_payment", "t1", payment_id=original, amount=env.payments[original]["amount"], reason="x")],
            [tool("refund_payment", "t2", payment_id=env.duplicates[small],
                  amount=env.payments[env.duplicates[small]]["amount"], reason="dup")],
            [text("ok")],
        ])
        ctx.model_factory = lambda _id: main
        default_driver(lab, ctx)
    assert "原始扣款" in json.dumps(main.requests[1]["messages"][-1], ensure_ascii=False)
    assert [r["payment_id"] for r in env.refunds] == [env.duplicates[small]]
