"""MCP chat tools (consume): list/call tools, resources, prompts + approval matrix."""
from __future__ import annotations

import pytest

from ivyea_agent import config, permission, tools_general
from ivyea_agent import mcp_client as mc
from ivyea_agent.agent_tools import READONLY_TOOLS, TOOL_SCHEMAS, ToolContext, dispatch


class FakeClient:
    def __init__(self, spec, **k):
        self.spec = spec

    def initialize(self):
        return {}

    def list_tools(self):
        return [{"name": "get_ads", "description": "pull ads"}]

    def call_tool(self, name, arguments=None):
        return {"content": [{"type": "text", "text": f"called {name} {arguments}"}]}

    def list_resources(self):
        return [{"uri": "x://1", "name": "R1"}]

    def read_resource(self, uri):
        return [{"text": f"body of {uri}"}]

    def list_prompts(self):
        return [{"name": "p1", "description": "d"}]

    def get_prompt(self, name, arguments=None):
        return {"messages": [{"role": "user", "content": {"type": "text", "text": "hi " + name}}]}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    monkeypatch.setattr(mc, "MCPClient", FakeClient)
    monkeypatch.setattr(config, "load_mcp", lambda: {"mcpServers": {
        "sorf": {"transport": "http", "url": "http://x"},
        "trusted_one": {"transport": "http", "url": "http://y", "trusted": True},
    }})


def test_list_tools_and_resources_and_prompts():
    ctx = ToolContext()
    assert "get_ads" in dispatch("mcp_list_tools", {"server": "sorf"}, ctx)
    assert "x://1" in dispatch("mcp_list_resources", {"server": "sorf"}, ctx)
    assert "body of x://1" in dispatch("mcp_read_resource", {"server": "sorf", "uri": "x://1"}, ctx)
    assert "p1" in dispatch("mcp_list_prompts", {"server": "sorf"}, ctx)
    assert "hi p1" in dispatch("mcp_get_prompt", {"server": "sorf", "name": "p1"}, ctx)


def test_list_all_servers_when_omitted():
    out = dispatch("mcp_list_tools", {}, ToolContext())
    assert "[sorf]" in out and "[trusted_one]" in out


def test_call_blocked_in_plan_mode():
    out = dispatch("mcp_call_tool", {"server": "sorf", "tool": "get_ads"}, ToolContext(plan_mode=True))
    assert "计划模式" in out


def test_call_trusted_server_auto_allows(monkeypatch):
    # request_intent must NOT be consulted for a trusted server
    monkeypatch.setattr(permission, "request_intent",
                        lambda *a, **k: pytest.fail("trusted server should not prompt"))
    out = dispatch("mcp_call_tool", {"server": "trusted_one", "tool": "get_ads"}, ToolContext())
    assert "called get_ads" in out


def test_call_untrusted_requires_approval(monkeypatch):
    monkeypatch.setattr(tools_general.permission, "request_intent", lambda *a, **k: permission.DENY)
    out = dispatch("mcp_call_tool", {"server": "sorf", "tool": "get_ads"}, ToolContext())
    assert "已跳过" in out
    monkeypatch.setattr(tools_general.permission, "request_intent", lambda *a, **k: permission.APPROVE)
    assert "called get_ads" in dispatch("mcp_call_tool", {"server": "sorf", "tool": "get_ads"}, ToolContext())


def test_unknown_server_is_graceful():
    assert "未配置 MCP 服务器" in dispatch("mcp_list_tools", {"server": "nope"}, ToolContext())


def test_registration_and_readonly_membership():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    six = {"mcp_list_tools", "mcp_call_tool", "mcp_list_resources",
           "mcp_read_resource", "mcp_list_prompts", "mcp_get_prompt"}
    assert six <= names
    assert "mcp_call_tool" not in READONLY_TOOLS                     # subagent can't call
    assert (six - {"mcp_call_tool"}) <= READONLY_TOOLS              # but can discover/read
