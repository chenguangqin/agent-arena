import httpx
import pytest

from devstore import scenarios
from devstore.server import DevStoreServer


def test_registry_lists_intro_scenario():
    assert "intro-refund" in scenarios.available()


def test_intro_store_rules():
    env = scenarios.get("intro-refund").build()
    with pytest.raises(ValueError):
        env.issue_refund("P-7004", 1.0, "already refunded")
    env.issue_refund("P-7002", 349.0, "captured on failed order")
    assert env.payments["P-7002"]["refunded"] == 349.0


def test_server_binds_loopback_requires_token_and_records_calls():
    env = scenarios.get("intro-refund").build()
    with DevStoreServer(env) as server:
        assert server.url.startswith("http://127.0.0.1:")
        assert httpx.post(server.url, json={}, timeout=5).status_code == 401
        wrong = httpx.post(server.url, json={}, headers={"Authorization": "Bearer nope"}, timeout=5)
        assert wrong.status_code == 401


def test_tool_errors_reach_the_client_with_their_message():
    from strands.tools.mcp import MCPClient

    env = scenarios.get("intro-refund").build()
    with DevStoreServer(env) as server, MCPClient(url=server.url, headers=server.headers) as client:
        result = client.call_tool_sync("t1", "get_order", {"order_id": "O-404"})
    assert result["status"] == "error"
    assert "O-404" in result["content"][0]["text"]
    assert server.calls[-1]["ok"] is False
