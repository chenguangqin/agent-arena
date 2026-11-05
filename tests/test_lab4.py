"""Lab4 driver and features with scripted models against the ticket-queue scenario over MCP."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import lab4
from devstore.scenarios import lab4_ticket_queue as scenario
from lab4 import LAB
from lab4.driver import driver
from tests.fakes import ScriptedModel, text, tool
from tests.helpers import features, lab_run

SKILLS = Path(lab4.__file__).resolve().parent / "skills"


def run(tmp_path, body, models):
    return lab_run(LAB, scenario, tmp_path, body, models)


def only(env, *ids):
    """Keep only some tickets in the queue."""
    env.tickets = [t for t in env.tickets if t["id"] in ids]


def copy_skills(ctx):
    shutil.copytree(SKILLS, ctx.agent_root / "lab4" / "skills")


def submit(tid):
    return tool("submit_resolution", f"s-{tid}", ticket_id=tid, summary="done")


def first_user_text(model) -> str:
    return json.dumps(model.requests[0]["messages"][0], ensure_ascii=False)


def test_scenario_queue_and_feedback():
    desk = scenario.build()
    assert desk.next_ticket()["id"] == "T-801"
    with pytest.raises(ValueError):
        desk.get_feedback("T-801")
    with pytest.raises(ValueError):
        desk.submit_resolution("T-802", "wrong ticket")
    desk.submit_resolution("T-801", "ok")
    assert desk.next_ticket()["id"] == "T-802"
    assert "处理合格" in desk.get_feedback("T-801")["feedback"]
    for tid in ("T-802", "T-803"):
        desk.submit_resolution(tid, "ok")
    assert "工单号" in desk.get_feedback("T-803")["feedback"]
    assert scenario.describe(desk)["resolved"] == 3


def test_driver_gives_each_ticket_a_fresh_context_and_skips_unsubmitted(tmp_path):
    first = ScriptedModel([[submit("T-801")], [text("done")]])
    second = ScriptedModel([[text("我不确定，先不提交")]])
    with run(tmp_path, "", []) as (lab, ctx, env):
        only(env, "T-801", "T-802")
        queue = [first, second]
        ctx.model_factory = lambda _id: queue.pop(0)
        result = driver(lab, ctx)
        ends = features(ctx, "ticket_end")
    assert [(e["ticket"], e["outcome"]) for e in ends] == [("T-801", "resolved"), ("T-802", "skipped")]
    assert len(second.requests[0]["messages"]) == 1  # nothing from T-801 carried over
    assert "T-801" not in first_user_text(second) and "T-802" in first_user_text(second)
    assert "T-802: skipped" in str(result.message)


def test_skills_hooks_list_names_and_load_on_demand(tmp_path):
    model = ScriptedModel([[tool("load_skill", "l1", skill="refund-policy")], [submit("T-803")], [text("ok")]])
    with run(tmp_path, '[skills]\nmode = "hooks"\n', []) as (lab, ctx, env):
        only(env, "T-803")
        copy_skills(ctx)
        ctx.model_factory = lambda _id: model
        driver(lab, ctx)
        loaded = features(ctx, "skill_loaded")
    system = model.requests[0]["system"]
    assert "refund-policy" in system and "escalation-policy" in system
    assert "200 元 < 金额" not in system  # bodies are not in the prompt
    assert loaded == [{**loaded[0], "skill": "refund-policy", "ticket": "T-803", "mode": "hooks"}]
    assert "200 元 < 金额" in json.dumps(model.requests[1]["messages"][-1], ensure_ascii=False)


def test_skills_sdk_uses_agent_skills_plugin(tmp_path):
    model = ScriptedModel([[tool("skills", "k1", skill_name="compensation-policy")], [submit("T-801")], [text("ok")]])
    with run(tmp_path, '[skills]\nmode = "sdk"\n', []) as (lab, ctx, env):
        only(env, "T-801")
        copy_skills(ctx)
        ctx.model_factory = lambda _id: model
        driver(lab, ctx)
        loaded = features(ctx, "skill_loaded")
    assert "skills" in model.requests[0]["tools"]
    assert "compensation-policy" in model.requests[0]["system"]
    assert loaded[0]["skill"] == "compensation-policy" and loaded[0]["mode"] == "sdk"
    assert "8 天及以上" in json.dumps(model.requests[1]["messages"][-1], ensure_ascii=False)


def test_sop_in_prompt_puts_full_policies_in_system_prompt(tmp_path):
    model = ScriptedModel([[submit("T-801")], [text("ok")]])
    with run(tmp_path, '[skills]\nsop_in_prompt = "on"\n', [model]) as (lab, ctx, env):
        only(env, "T-801")
        driver(lab, ctx)
    system = model.requests[0]["system"]
    assert "SOP-REFUND" in system and "SOP-ESCALATION" in system and "SOP-COMPENSATION" in system


def test_facts_survive_fresh_contexts_and_are_injected_for_the_same_customer(tmp_path):
    t801 = ScriptedModel([
        [tool("remember", "r1", fact="客户只接受短信通知，不要用 App 推送或邮件", about="C5001")],
        [submit("T-801")], [text("ok")],
    ])
    t807 = ScriptedModel([[submit("T-807")], [text("ok")]])
    with run(tmp_path, '[memory]\nfacts = "hooks"\n', []) as (lab, ctx, env):
        only(env, "T-801", "T-807")
        queue = [t801, t807]
        ctx.model_factory = lambda _id: queue.pop(0)
        driver(lab, ctx)
        injected = features(ctx, "memory_injected")
        facts = (ctx.agent_root / "lab4" / "memory" / "facts.md").read_text(encoding="utf-8")
    assert "来源 T-801" in facts
    assert injected[0]["ticket"] == "T-807" and injected[0]["entries"][0]["from"] == "T-801"
    assert "只接受短信" in first_user_text(t807)

    # A new run starts from the committed (empty) memory: nothing leaks across runs.
    fresh = ScriptedModel([[submit("T-807")], [text("ok")]])
    with run(tmp_path / "next", '[memory]\nfacts = "hooks"\n', [fresh]) as (lab, ctx, env):
        only(env, "T-807")
        driver(lab, ctx)
        assert features(ctx, "memory_injected") == []


def test_lessons_from_feedback_apply_to_later_tickets(tmp_path):
    t803 = ScriptedModel([[submit("T-803")], [text("ok")]])
    distiller = ScriptedModel([[tool("Lesson", "x1", rule="回复客户时第一行写【工单号】", supersedes=None)]])
    t804 = ScriptedModel([[submit("T-804")], [text("ok")]])
    with run(tmp_path, '[memory]\nlessons = "on"\n', [t803, distiller, t804]) as (lab, ctx, env):
        only(env, "T-803", "T-804")
        driver(lab, ctx)
        saved = features(ctx, "lesson_saved")
        book = (ctx.agent_root / "lab4" / "memory" / "lessons.md").read_text(encoding="utf-8")
    assert saved[0]["ticket"] == "T-803" and "工单号" in saved[0]["feedback"]
    assert "来源 T-803" in book
    assert "【工单号】" in first_user_text(t804)


def test_lesson_book_marks_superseded_rules(tmp_path):
    from lab4.features.lessons import LessonBook

    book = LessonBook(tmp_path / "lessons.md")
    book.add("规则一", "T-1", None)
    book.add("规则二", "T-2", 1)
    assert book.active() == [line for line in book.lines() if "规则二" in line]


def test_memory_sdk_installs_memory_manager_tools(tmp_path):
    model = ScriptedModel([[submit("T-801")], [text("ok")]])
    with run(tmp_path, '[memory]\nfacts = "sdk"\n', [model]) as (lab, ctx, env):
        only(env, "T-801")
        driver(lab, ctx)
        event = features(ctx, "memory_manager")
    assert event and {"search_memory", "add_memory"} <= set(model.requests[0]["tools"])


def test_induce_skill_writes_candidate_from_trace(tmp_path):
    from lab4.induce_skill import main

    model = ScriptedModel([
        [tool("get_payments", "p1", order_id="O-7002")],
        [tool("issue_refund", "p2", payment_id="P-9022", amount=298.0, reason="dup")],
        [submit("T-802")], [text("ok")],
    ])
    with run(tmp_path, "", [model]) as (lab, ctx, env):
        only(env, "T-802")
        driver(lab, ctx)
        trace = ctx.trace.path
    out = tmp_path / "candidates"
    assert main(["--trace", str(trace), "--ticket", "T-802", "--name", "duplicate-charge",
                 "--description", "重复扣款", "--out", str(out)]) == 0
    skill = (out / "duplicate-charge" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: duplicate-charge") and "issue_refund" in skill
