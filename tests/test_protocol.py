"""End-to-end: protocol entry → devstore MCP server → scripted model (no Bedrock)."""

import json

from core.config import load_profile
from core.protocol import run_request
from devstore import scenarios
from devstore.server import DevStoreServer
from lab1 import LAB
from tests.fakes import ScriptedModel, text, tool


def _request(tmp_path, instruction):
    return {
        "schema_version": "0.2",
        "task": {"id": "intro-refund", "instruction": instruction},
        "agent_config": {"profile": "baseline"},
        "paths": {"agent_root": str(tmp_path)},
    }


def test_lab1_runs_scenario_through_protocol(tmp_path, monkeypatch):
    scenario = scenarios.get("intro-refund")
    env = scenario.build()
    model = ScriptedModel(
        [
            [tool("get_ticket", "t1", ticket_id="T-501")],
            [tool("issue_refund", "t2", payment_id="P-7002", amount=349.0, reason="captured")],
            [
                tool("send_notification", "t3", customer_id="C1024", message="已退款"),
                tool("close_ticket", "t4", ticket_id="T-501", resolution="refunded"),
            ],
            [text("完成。")],
        ]
    )
    with DevStoreServer(env) as server:
        monkeypatch.setenv("AGENT_EVAL_MCP_URL", server.url)
        monkeypatch.setenv("AGENT_EVAL_MCP_HEADERS", json.dumps(server.headers))
        monkeypatch.setenv("AGENT_EVAL_MCP_TOKEN", server.token)
        config = load_profile("baseline", LAB.profiles_dir, LAB.schema)
        result = run_request(LAB, _request(tmp_path, scenario.INSTRUCTION), config,
                             model_factory=lambda _id: model)
        calls = [c["name"] for c in server.calls]

    assert result["status"] == "completed" and result["final_text"] == "完成。"
    assert result["usage"]["input_tokens"] == 400  # 4 calls × 100 from the scripted model
    assert env.tickets["T-501"]["status"] == "closed"
    assert calls[:2] == ["get_ticket", "issue_refund"]

    trace_text = (tmp_path / "reports/trace.jsonl").read_text()
    assert server.token not in trace_text
    events = [json.loads(line) for line in trace_text.splitlines()]
    model_calls = [e for e in events if e["event"] == "model_call"]
    assert [e["context_tokens"] for e in model_calls] == [100, 100, 100, 100]
    summary = json.loads((tmp_path / "reports/summary.json").read_text())
    assert summary["tool_calls"] == 4 and summary["features"] == []
    assert summary["usage"]["model_calls"] == 4
