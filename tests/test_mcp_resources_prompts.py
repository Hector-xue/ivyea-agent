"""MCP client resources/prompts methods + reverse-server resources/prompts."""
from __future__ import annotations

from ivyea_agent import mcp_server
from ivyea_agent.mcp_client import MCPClient


# ── client ────────────────────────────────────────────────────────────────
def _client_recording():
    c = MCPClient({"transport": "http", "url": "http://x"})
    calls = []

    def fake_rpc(method, params=None):
        calls.append((method, params))
        return {"resources": [{"uri": "u"}], "contents": [{"text": "b"}],
                "prompts": [{"name": "p"}], "messages": []}

    c._rpc = fake_rpc
    return c, calls


def test_client_methods_route_to_correct_jsonrpc():
    c, calls = _client_recording()
    assert c.list_resources() == [{"uri": "u"}]
    assert c.read_resource("u://1") == [{"text": "b"}]
    assert c.list_prompts() == [{"name": "p"}]
    c.get_prompt("p", {"a": 1})
    assert [m for m, _ in calls] == ["resources/list", "resources/read", "prompts/list", "prompts/get"]
    assert calls[1][1] == {"uri": "u://1"}
    assert calls[3][1] == {"name": "p", "arguments": {"a": 1}}


# ── reverse server ──────────────────────────────────────────────────────────
def _call(method, params=None):
    res = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return res["result"]


def test_initialize_advertises_resources_and_prompts():
    caps = _call("initialize")["capabilities"]
    assert {"tools", "resources", "prompts"} <= set(caps)


def test_resources_list_and_read_knowledge_cards():
    resources = _call("resources/list")["resources"]
    assert resources and all(r["uri"].startswith("ivyea-knowledge://") for r in resources)
    body = _call("resources/read", {"uri": resources[0]["uri"]})["contents"][0]
    assert body["mimeType"] == "text/markdown" and body["text"]


def test_prompts_list_and_get_skills():
    prompts = _call("prompts/list")["prompts"]
    assert prompts
    got = _call("prompts/get", {"name": prompts[0]["name"]})
    assert got["messages"][0]["role"] == "user"
    assert got["messages"][0]["content"]["text"]


def test_unknown_resource_and_prompt_error():
    err = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "ivyea-knowledge://nope"}})
    assert "error" in err
    err2 = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "prompts/get", "params": {"name": "nope"}})
    assert "error" in err2
