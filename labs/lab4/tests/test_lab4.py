"""Lab4 starter and reference solution, driven by scripted models (no Bedrock)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent as starter
from labkit.cli import run_queue, run_ticket
from solution import agent as solution
from solution import memory
from solution.memory import Candidate, MemoryStore, govern
from tests.conftest import ModelQueue, events
from tests.fakes import ScriptedModel, text, tool


def submit(ticket_id, reply="已处理"):
    return [[tool("submit_resolution", f"s-{ticket_id}", ticket_id=ticket_id, summary="ok")], [text(reply)]]


def test_each_ticket_runs_in_a_fresh_session(env, tmp_path, monkeypatch):
    m1, m2 = ScriptedModel(submit("T-801")), ScriptedModel(submit("T-802"))
    monkeypatch.setattr(starter, "build_model", ModelQueue([m1, m2]))
    run_ticket(env, starter.build_agent, starter.on_session_end, "baseline", tmp_path / "mem", tmp_path, 1)
    run_ticket(env, starter.build_agent, starter.on_session_end, "baseline", tmp_path / "mem", tmp_path, 2)
    first_of_second = json.dumps(m2.requests[0]["messages"], ensure_ascii=False)
    assert len(m2.requests[0]["messages"]) == 1  # empty context apart from the ticket prompt
    assert "T-802" in first_of_second and "T-801" not in first_of_second
    assert env.state.tickets[0]["status"] == env.state.tickets[1]["status"] == "resolved"


def test_ticket_not_submitted_is_skipped_and_trajectory_recorded(env, tmp_path, monkeypatch):
    model = ScriptedModel([[tool("get_order", "o1", order_id="O-7001")], [text("先不提交")]])
    monkeypatch.setattr(starter, "build_model", ModelQueue([model]))
    seen = []
    session = run_ticket(env, starter.build_agent, lambda e, m, s: seen.append(s), "baseline", tmp_path / "mem",
                         tmp_path, 1, reports=tmp_path / "sessions")
    assert env.state.tickets[0]["status"] == "skipped"
    assert session.trajectory.submitted is False
    assert [c["name"] for c in session.trajectory.tool_calls] == ["get_order"]
    assert "O-7001" in session.trajectory.tool_calls[0]["result"]
    assert seen == [session] and (tmp_path / "sessions" / "T-801.json").exists()


def test_skills_mode_loads_skill_and_resource_but_hides_policies(env, tmp_path, monkeypatch):
    model = ScriptedModel([
        [tool("skills", "k1", skill_name="compensation-policy")],
        [tool("read_skill_file", "r1", path="compensation-policy/references/compensation_table.md")],
        [tool("read_skill_file", "r2", path="../agent.py")],
        *submit("T-801"),
    ])
    monkeypatch.setattr(solution, "build_model", ModelQueue([model]))
    session = run_ticket(env, solution.build_agent, solution.on_session_end, "skills", tmp_path / "mem",
                         solution.SKILLS_DIR, 1)
    tools = model.requests[0]["tools"]
    assert "skills" in tools and "read_skill_file" in tools and "read_policy" not in tools
    assert "compensation-policy" in model.requests[0]["system"]  # metadata is always in the prompt
    assert "SOP-COMPENSATION" not in model.requests[0]["system"]  # the body is not
    calls = session.trajectory.tool_calls
    assert "references/compensation_table.md" in calls[0]["result"]  # resources are listed with the body
    assert "8 天及以上" in calls[1]["result"]
    assert calls[2]["status"] == "error"  # cannot read outside the skills directory


def test_sop_in_prompt_puts_every_policy_in_the_system_prompt(env, tmp_path, monkeypatch):
    model = ScriptedModel(submit("T-801"))
    monkeypatch.setattr(solution, "build_model", ModelQueue([model]))
    run_ticket(env, solution.build_agent, solution.on_session_end, "sop_in_prompt", tmp_path / "m", tmp_path, 1)
    assert model.requests[0]["system"].count("附录 A") == 3


def candidate(**kw):
    base = {"kind": "fact", "subject": "PG-B", "content": "PG-B 网关超时重试会再扣一次款，重试扣款不关联订单，"
            "要在支付日志里按订单号查找后退款", "confidence": 0.9, "relation": "new", "keywords": ["扣款", "多扣"]}
    return Candidate(**{**base, **kw})


def test_governance_admission_dedupe_disambiguation_and_forgetting(tmp_path):
    store = MemoryStore(tmp_path)
    log = govern(store, [candidate(), candidate(content="可能需要客户补充信息", confidence=0.3)], "T-802")
    assert [entry["action"] for entry in log] == ["added", "rejected"]
    # The model says "new" but it is the same fact in other words → merged, not appended.
    log = govern(store, [candidate(content="PG-B 网关超时重试会再扣一次款，重试扣款不关联订单，需在日志中按订单号查找",
                                   keywords=["两笔"])], "T-806")
    assert log[0]["action"] == "merged" and len(store.items) == 1
    assert store.items[0].sources == ["T-802", "T-806"] and "两笔" in store.items[0].keywords
    # A newer statement that contradicts the old one supersedes it.
    log = govern(store, [candidate(content="INC-311 已修复，PG-B 不再重复扣款", relation="contradicts",
                                   target_id="M-001")], "T-830")
    assert store.items[0].status == "superseded" and log[0]["new"] == "M-002"
    # An unused, low-confidence memory is forgotten after enough idle sessions.
    store.add("fact", "全局", "某条用处不大的观察", "T-803", 0.65)
    store.sessions += 10
    assert [m.id for m in store.forget_stale()] == ["M-003"]
    store.save()
    reloaded = MemoryStore(tmp_path)
    assert [(m.id, m.status) for m in reloaded.items] == [("M-001", "superseded"), ("M-002", "active"),
                                                          ("M-003", "forgotten")]
    assert reloaded.items[1].keywords == ["扣款", "多扣"]


def test_retrieval_uses_triggers_subjects_and_global_rules(tmp_path):
    store = MemoryStore(tmp_path)
    store.add("fact", "PG-B", "PG-B 重试扣款不关联订单，到支付日志中查找", "T-802", 0.9, ["多扣", "两笔", "扣了两次"])
    store.add("preference", "C5001", "只接受短信联系", "T-801", 0.95, ["C5001"])
    store.add("lesson", "全局", "回复第一行写明工单号【T-xxx】", "T-803", 0.95, [])
    store.add("fact", "物流", "转运中心滞留通常 2 天", "T-804", 0.7, ["滞留"])
    store.save()
    vague = {"id": "T-809", "customer_id": "C5009", "order_id": "O-7009", "subject": "扣款疑问",
             "body": "好像被多扣了一笔钱，麻烦看一下。"}
    hits = {m.subject for m in MemoryStore(tmp_path).relevant(vague)}
    assert hits == {"PG-B", "全局"}
    repeat_customer = {"id": "T-807", "customer_id": "C5001", "order_id": "O-7007", "subject": "少发配件", "body": "少了充电线"}
    assert "C5001" in {m.subject for m in MemoryStore(tmp_path).relevant(repeat_customer)}


def test_full_pipeline_extracts_from_trajectory_and_injects_into_next_session(env, tmp_path, monkeypatch):
    extracted = ScriptedModel([[tool("Extraction", "x1", items=[
        {"kind": "preference", "subject": "C5001", "content": "客户 C5001 只接受短信联系", "confidence": 0.95,
         "relation": "new", "keywords": ["C5001"]},
        {"kind": "lesson", "subject": "全局", "content": "回复第一行写工单号", "confidence": 0.9, "relation": "new"},
    ])]])
    t1, t2 = ScriptedModel(submit("T-801")), ScriptedModel(submit("T-802"))
    second_extraction = ScriptedModel([[tool("Extraction", "x2", items=[])]])
    monkeypatch.setattr(solution, "build_model", ModelQueue([t1, t2]))
    monkeypatch.setattr(memory, "MODEL_FACTORY", ModelQueue([extracted, second_extraction]))
    monkeypatch.setattr(solution, "SKILLS_DIR", solution.SKILLS_DIR)
    mem = tmp_path / "memory"
    sessions = run_queue_limited(env, mem, 2)
    assert (mem / "trajectories" / "T-801.json").exists()  # 采集
    extractor_input = json.dumps(extracted.requests[0]["messages"], ensure_ascii=False)
    assert "只发短信" in extractor_input  # 抽取 sees the trajectory, including the customer's words
    assert "回复第一行写工单号" in t2.requests[0]["system"]  # 使用: the global lesson reaches the next session
    assert "C5001" not in t2.requests[0]["system"]  # the preference is about another customer
    assert events(tmp_path, "memory_injected")[0]["entries"][0]["from"] == ["T-801"]
    store = MemoryStore(mem)
    assert len(store.items) == 2 and store.sessions == 2
    assert sessions[1].trajectory.submitted


def run_queue_limited(env, mem, n):
    sessions = []
    for i in range(1, n + 1):
        sessions.append(run_ticket(env, solution.build_agent, solution.on_session_end, "full", mem,
                                   solution.SKILLS_DIR, i))
    return sessions


def test_forget_and_memory_commands(tmp_path, capsys):
    store = MemoryStore(tmp_path)
    store.add("preference", "C5001", "只接受短信联系", "T-801", 0.95, ["C5001"])
    store.save()
    from labkit import CommandContext

    ctx = CommandContext(env=None, mode="full", memory_dir=tmp_path, skills_dir=Path("."), last_session=None)
    memory.forget_command("短信", ctx)
    memory.show_memory("", ctx)
    assert "共 0 条有效记忆" in capsys.readouterr().out
    assert MemoryStore(tmp_path).items[0].status == "forgotten"


def test_queue_runs_until_empty(env, tmp_path, monkeypatch):
    models = [ScriptedModel(submit(f"T-8{n:02d}")) for n in range(1, 11)]
    monkeypatch.setattr(starter, "build_model", ModelQueue(models))
    sessions = run_queue(env, starter.build_agent, starter.on_session_end, "baseline", tmp_path / "m", tmp_path)
    assert len(sessions) == 10 and env.describe()["resolved"] == 10


@pytest.mark.parametrize("module", [starter, solution])
def test_commands_are_callables(module):
    assert all(callable(fn) for fn in module.COMMANDS.values())


def test_recall_tool_finds_memory_by_system_name(tmp_path, env):
    from labkit import Session

    store = MemoryStore(tmp_path)
    store.add("fact", "PG-B", "PG-B 重试扣款不关联订单，用 search_logs 搜订单号找到多扣的一笔", "T-802", 0.9, ["多扣"])
    store.save()
    recall = memory.make_recall(Session(6, {"id": "T-806"}, tmp_path, tmp_path))
    assert recall._tool_func("PG-B")[0]["subject"] == "PG-B"


def test_log_search_requires_all_terms(env):
    assert env.call("search_logs", query="O-7009 PG-B")  # multi-word queries match lines containing every term
    assert env.call("search_logs", query="O-7009 nonexistent") == []
