"""Workflow engine and runner, driven by fake node runners and scripted models (no Bedrock)."""

from __future__ import annotations

import json
import threading
import time

import pytest

from solution.checks import NotifyLedger, completion_problems
from solution.runner import WorkflowRunner
from solution.workflow import Engine, Step, Workflow, validate
from tests.conftest import events
from tests.fakes import ScriptedModel, text, tool

TOOLS = {"list_alerts", "get_order", "replay_order", "issue_refund", "notify_customer"}


def wf(*steps: dict) -> Workflow:
    return Workflow(goal="t", steps=[Step(**s) for s in steps])


# ---- validation -------------------------------------------------------------------------

@pytest.mark.parametrize("steps, message", [
    ([{"id": "a", "instruction": "x"}, {"id": "a", "instruction": "y"}], "重复"),
    ([{"id": "a", "instruction": "x", "depends_on": ["zz"]}], "不存在"),
    ([{"id": "a", "instruction": "x", "tools": ["rm_rf"]}], "未知工具"),
    ([{"id": "a", "instruction": "x", "depends_on": ["b"]}, {"id": "b", "instruction": "y", "depends_on": ["a"]}],
     "循环"),
    ([{"id": "a", "instruction": "x"}, {"id": "b", "kind": "foreach", "instruction": "y", "items_from": "a"}],
     "depends_on"),
    ([{"id": "a", "instruction": "x"},
      {"id": "b", "kind": "foreach", "instruction": "y", "items_from": "a", "depends_on": ["a"]}], "output=json"),
    ([{"id": "Bad-Id", "instruction": "x"}], "小写"),
])
def test_validate_rejects(steps, message):
    errors = validate(wf(*steps), TOOLS)
    assert any(message in e for e in errors), errors


def test_validate_allows_references_to_completed_steps():
    workflow = wf({"id": "retry", "kind": "foreach", "instruction": "x", "items_from": "orders",
                   "depends_on": ["orders"], "tools": ["replay_order"]})
    assert validate(workflow, TOOLS, done={"orders"}) == []
    assert validate(wf({"id": "orders", "instruction": "again"}), TOOLS, done={"orders"})  # no re-running


# ---- engine ---------------------------------------------------------------------------------

def test_dependencies_order_and_independent_steps_run_in_parallel():
    barrier = threading.Barrier(2, timeout=5)  # b and c must be running at the same time
    started: list[str] = []

    def run_node(step, label, prompt):
        started.append(step.id)
        if step.id in ("b", "c"):
            barrier.wait()
        if step.id == "d":
            assert "### b" in prompt and "### c" in prompt  # sees both dependency results
        return f"{step.id} done"

    results: dict = {}
    failure = Engine(run_node).run(wf(
        {"id": "a", "instruction": "x"},
        {"id": "b", "instruction": "x", "depends_on": ["a"]},
        {"id": "c", "instruction": "x", "depends_on": ["a"]},
        {"id": "d", "instruction": "x", "depends_on": ["b", "c"]},
    ), results)
    assert failure is None
    assert started[0] == "a" and started[-1] == "d" and set(started[1:3]) == {"b", "c"}
    assert list(results) == ["a", "b", "c", "d"] or set(results) == {"a", "b", "c", "d"}


def test_foreach_fans_out_in_batches_with_filter():
    labels: list[str] = []

    def run_node(step, label, prompt):
        if step.id == "orders":
            return json.dumps([{"id": f"O{i}", "kind": "replay" if i % 3 else "refund"} for i in range(10)])
        labels.append(label)
        batch = json.loads(prompt.split("条）\n", 1)[1].split("\n\n", 1)[0])
        return "\n".join(json.dumps({"id": item["id"], "outcome": "done"}) for item in batch)

    results: dict = {}
    failure = Engine(run_node).run(wf(
        {"id": "orders", "instruction": "list", "output": "json"},
        {"id": "process", "kind": "foreach", "instruction": "p", "items_from": "orders", "depends_on": ["orders"],
         "where": {"kind": "replay"}, "batch_size": 3},
    ), results)
    assert failure is None
    assert sorted(labels) == ["process#1", "process#2"]  # 6 replay items / 3 per batch
    assert len(results["process"]) == 6 and all(r["outcome"] == "done" for r in results["process"])


def test_failure_stops_new_steps_but_keeps_finished_results():
    def run_node(step, label, prompt):
        if step.id == "bad":
            return "FAILED: 503 replay service unavailable"
        if step.id == "slow":
            time.sleep(0.2)
        return "ok"

    results: dict = {}
    failure = Engine(run_node).run(wf(
        {"id": "bad", "instruction": "x"},
        {"id": "slow", "instruction": "x"},
        {"id": "after", "instruction": "x", "depends_on": ["bad"]},
    ), results)
    assert failure is not None and failure.step == "bad" and "503" in failure.reason
    assert "slow" in results and "after" not in results


def test_json_step_without_json_fails():
    failure = Engine(lambda s, label, p: "no json here").run(wf({"id": "a", "instruction": "x", "output": "json"}), {})
    assert failure is not None and "JSON" in failure.reason


# ---- completion checks ---------------------------------------------------------------------

def test_completion_problems_reflect_environment_state(env):
    ledger = NotifyLedger()
    problems = completion_problems(env, ledger)
    assert any("折扣规则" in p for p in problems) and any("复盘报告" in p for p in problems)
    state = env.state
    state.rules["PROMO-1111-STACK"]["max_discount_pct"] = 50
    for order in state.orders.values():
        if order["failed_at"]:
            order["status"] = "completed"
            ledger.sent[order["customer_id"]] += 1
    for ticket in state.tickets.values():
        ticket["status"] = "closed"
    state.report = "## 时间线\n## 影响范围\n## 处理结果"
    assert completion_problems(env, ledger) == []


def test_ledger_blocks_second_notification(env, tmp_path):
    from strands import Agent

    from labkit import trace

    model = ScriptedModel([
        [tool("notify_customer", "n1", customer_id="C3001", message="已处理")],
        [tool("notify_customer", "n2", customer_id="C3001", message="再次")],
        [text("ok")],
    ])
    agent = Agent(model=model, tools=[env.mcp_client(tool_filters={"allowed": ["notify_customer"]})],
                  hooks=[NotifyLedger(), trace.Tracer()], callback_handler=None)
    agent("go")
    agent.cleanup()
    assert len(env.state.notifications) == 1
    assert events(tmp_path, "duplicate_notification_blocked")


# ---- the runner ------------------------------------------------------------------------------

class Models:
    """model_factory for the runner: one scripted model per agent label."""

    def __init__(self, scripts: dict[str, list]) -> None:
        self.models = {label: ScriptedModel(turns) for label, turns in scripts.items()}

    def __call__(self, label: str = "main", **_):
        return self.models[label]


def plan(call_id: str, *steps: dict) -> list:
    return [tool("Workflow", call_id, goal="g", steps=list(steps))]


def test_runner_replans_after_a_failed_step_and_reports_completion_problems(env, tmp_path):
    models = Models({
        "orchestrator": [
            plan("w1", {"id": "diagnose", "instruction": "看告警", "tools": ["list_alerts"]},
                 {"id": "fix", "instruction": "修规则", "tools": ["list_alerts"], "depends_on": ["diagnose"]}),
            plan("w2", {"id": "fix_again", "instruction": "换个方法修", "depends_on": ["diagnose"]}),
            plan("w3", {"id": "noop", "instruction": "结束"}),
        ],
        "diagnose": [[tool("list_alerts", "a1")], [text("规则 PROMO-1111-STACK 可疑")]],
        "fix": [[text("FAILED: 没有修改规则的工具")]],
        "fix_again": [[text("仍然无法修改")]],
        "noop": [[text("done")]],
    })
    runner = WorkflowRunner(env, model_factory=models, max_rounds=3)
    reply = runner("处理事故")
    runner.cleanup()

    orchestrator_inputs = [json.dumps(r["messages"], ensure_ascii=False) for r in models.models["orchestrator"].requests]
    assert "步骤 fix 失败" in orchestrator_inputs[1] and "diagnose" in orchestrator_inputs[1]
    assert "完成检查未通过" in orchestrator_inputs[2]
    assert "未能在 3 轮内完成" in reply
    # Nodes only saw their own tools; the orchestrator saw none.
    assert models.models["diagnose"].requests[0]["tools"] == ["list_alerts"]
    assert models.models["orchestrator"].requests[0]["tools"] == ["Workflow"]
    assert [e["round"] for e in events(tmp_path, "workflow")] == [1, 2, 3]


def test_runner_rejects_invalid_workflow_and_asks_again(env, tmp_path):
    models = Models({
        "orchestrator": [
            plan("w1", {"id": "a", "instruction": "x", "tools": ["drop_database"]}),
            plan("w2", {"id": "a", "instruction": "x"}),
        ],
        "a": [[text("done")]],
    })
    runner = WorkflowRunner(env, model_factory=models, max_rounds=1)  # invalid plans do not use up rounds
    runner("t")
    assert events(tmp_path, "workflow_invalid")[0]["errors"][0].startswith("a: 未知工具")
    assert [e["round"] for e in events(tmp_path, "workflow")] == [1]


def test_normalize_makes_foreach_sources_json():
    from solution.workflow import normalize

    workflow = normalize(wf({"id": "a", "instruction": "x"},
                            {"id": "b", "kind": "foreach", "instruction": "y", "items_from": "a", "depends_on": ["a"]}))
    assert workflow.steps[0].output == "json" and validate(workflow, TOOLS) == []


def test_tool_foreach_where_every_call_fails_reports_fields(env):
    failure = Engine(lambda *a: "", call_tool=env.call).run(wf(
        {"id": "orders", "kind": "tool", "tool": "list_failed_orders"},
        {"id": "details", "kind": "foreach", "tool": "get_order", "arguments": {"order_id": "{order_id}"},
         "items_from": "orders", "depends_on": ["orders"]},
    ), {})
    assert failure is not None and "'customer_id'" in failure.reason and "占位符" in failure.reason


def test_starter_baseline_builds_and_workflow_is_a_todo(env):
    import agent as starter

    assert starter.build_agent(env, "baseline") is not None
    with pytest.raises(NotImplementedError):
        starter.build_agent(env, "workflow")("task")


def test_tool_step_pages_deterministically_and_foreach_dedups(env):
    seen: list[list[str]] = []

    def run_node(step, label, prompt):
        batch = json.loads(prompt.split("条）\n", 1)[1].split("\n\n", 1)[0])
        seen.append([item["customer_id"] for item in batch])
        return "\n".join(json.dumps({"customer_id": i["customer_id"], "outcome": "notified"}) for i in batch)

    results: dict = {}
    failure = Engine(run_node, call_tool=env.call).run(wf(
        {"id": "orders", "kind": "tool", "tool": "list_failed_orders", "paginate": True},
        {"id": "notify", "kind": "foreach", "instruction": "通知", "items_from": "orders", "depends_on": ["orders"],
         "unique_by": "customer_id", "batch_size": 10},
    ), results)
    assert failure is None
    assert len(results["orders"]) == 40  # all three pages, no model involved
    customers = [c for batch in seen for c in batch]
    assert len(customers) == len(set(customers)) == 30


def test_where_that_matches_nothing_fails_with_the_real_field_names():
    def run_node(step, label, prompt):
        return json.dumps([{"order_id": "O1", "status": "failed"}])

    failure = Engine(run_node).run(wf(
        {"id": "orders", "instruction": "list", "output": "json"},
        {"id": "replay", "kind": "foreach", "instruction": "r", "items_from": "orders", "depends_on": ["orders"],
         "where": {"payment_status": "captured"}},
    ), {})
    assert failure is not None and failure.step == "replay"
    assert "order_id" in failure.reason and "status" in failure.reason


def test_tool_step_requires_a_known_tool():
    errors = validate(wf({"id": "q", "kind": "tool", "tool": "drop_all"}), TOOLS)
    assert any("tool 步骤" in e for e in errors)


def test_foreach_tool_calls_once_per_item_with_placeholders(env):
    results: dict = {}
    failure = Engine(lambda *a: "unused", call_tool=env.call).run(wf(
        {"id": "orders", "kind": "tool", "tool": "list_failed_orders", "arguments": {"page": 1}},
        {"id": "details", "kind": "foreach", "tool": "get_order", "arguments": {"order_id": "{id}"},
         "items_from": "orders", "depends_on": ["orders"]},
    ), results)
    assert failure is None
    assert len(results["details"]) == 15 and all(r["status"] == "ok" for r in results["details"])
    assert results["details"][0]["result"]["payment_id"].startswith("P-")


def test_refund_guard_requires_a_sold_out_replay(env, tmp_path):
    from strands import Agent

    from labkit import trace
    from solution.checks import RefundGuard

    state = env.state
    healthy = next(o for o in state.orders.values() if o["failed_at"] and not o["_sold_out"])
    sold = next(o for o in state.orders.values() if o["failed_at"] and o["_sold_out"])
    state.rules["PROMO-1111-STACK"]["max_discount_pct"] = 50
    state.replay_calls = 100  # past the 503 window
    model = ScriptedModel([
        [tool("get_order", "g1", order_id=healthy["id"]), tool("get_order", "g2", order_id=sold["id"])],
        [tool("issue_refund", "r1", payment_id=healthy["payment_id"], amount=healthy["amount"], reason="x")],
        [tool("replay_order", "p1", order_id=sold["id"])],
        [tool("issue_refund", "r2", payment_id=sold["payment_id"], amount=sold["amount"], reason="sold out")],
        [text("ok")],
    ])
    allowed = ["get_order", "replay_order", "issue_refund"]
    agent = Agent(model=model, tools=[env.mcp_client(tool_filters={"allowed": allowed})],
                  hooks=[RefundGuard(), trace.Tracer()], callback_handler=None)
    agent("go")
    agent.cleanup()
    assert state.orders[healthy["id"]]["status"] == "failed"  # blocked: never replayed
    assert state.orders[sold["id"]]["status"] == "refunded"
    assert events(tmp_path, "refund_blocked")[0]["order_id"] == healthy["id"]


def test_ticket_guard_blocks_unrelated_tickets():
    from strands import Agent

    from labkit import Env, trace
    from solution.checks import TicketGuard
    from tests.fakes import ScriptedModel, text, tool

    trace.configure(None, echo=False)
    env = Env.local()
    try:
        affected = next(t for t in env.state.tickets.values() if "大促" in t["subject"])
        model = ScriptedModel([
            [tool("list_tickets", "t1", status="open")],
            [tool("list_failed_orders", "t2", page=1), tool("list_failed_orders", "t3", page=2),
             tool("list_failed_orders", "t4", page=3)],
            [tool("close_ticket", "t5", ticket_id="T-799", resolution="x")],
            [tool("close_ticket", "t6", ticket_id=affected["id"], resolution="refunded")],
            [text("ok")],
        ])
        agent = Agent(model=model, tools=[env.mcp_client()], hooks=[TicketGuard()], callback_handler=None)
        agent("go")
        agent.cleanup()
        assert env.state.tickets["T-799"]["status"] == "open"
        assert env.state.tickets[affected["id"]]["status"] == "closed"
    finally:
        env.close()
