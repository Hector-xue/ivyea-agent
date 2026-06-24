"""MCP writeActions templates and validation."""
from __future__ import annotations

import io
import json


def test_mcp_write_template_validates():
    from ivyea_agent import mcp_write

    spec = dict(mcp_write.WRITE_ACTIONS_TEMPLATE)
    assert mcp_write.validate_spec(spec) == []
    text = mcp_write.template_json()
    assert "negative_rollback" in text
    assert "{search_term}" in text


def test_mcp_write_validation_errors():
    from ivyea_agent import mcp_write

    errors = mcp_write.validate_spec({"writeActions": {"negative": {"args": {}}}})
    assert "writeActions.negative.tool 为空" in errors
    assert "缺少 writeActions.bid" in errors


def test_cli_mcp_template_and_validate(ivyea_home, capsys):
    from ivyea_agent import config, mcp_write
    from ivyea_agent.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["mcp", "template"])
    assert args.func(args) == 0
    assert "writeActions" in capsys.readouterr().out

    config.mcp_set_server("writer", {"transport": "http", "url": "http://x", **mcp_write.WRITE_ACTIONS_TEMPLATE})
    args = parser.parse_args(["mcp", "validate", "writer"])
    assert args.func(args) == 0
    assert "OK MCP 写入映射" in capsys.readouterr().out


def test_mcp_data_source_suggestion():
    from ivyea_agent import mcp_source

    result = {
        "structuredContent": {
            "data": {
                "rows": [{
                    "date": "2026-06-01",
                    "asin": "B0X",
                    "campaignName": "SP Auto",
                    "searchTerm": "karaoke machine",
                    "clicks": 12,
                    "spend": 8.5,
                    "orders": 1,
                    "sales": 39.99,
                }]
            }
        }
    }
    suggestion = mcp_source.suggest_data_source("get_report", {"asin": "{asin}"}, result)
    ds = suggestion["dataSource"]
    assert ds["rows_path"] == "data.rows"
    assert ds["field_map"]["ASIN"] == "asin"
    assert ds["field_map"]["Customer Search Term"] == "searchTerm"
    assert suggestion["coverage"]["mapped"] >= 7


def test_cli_mcp_suggest(ivyea_home, monkeypatch, capsys):
    from ivyea_agent import config, mcp_client
    from ivyea_agent.cli import build_parser

    config.mcp_set_server("reporter", {"transport": "http", "url": "http://mcp.test"})
    calls = []

    def fake_rpc(self, method, params=None):
        calls.append(method)
        if method == "initialize":
            return {}
        if method == "tools/call":
            return {
                "structuredContent": {
                    "data": {
                        "rows": [{
                            "date": "2026-06-01",
                            "asin": "B0X",
                            "campaignName": "SP Auto",
                            "searchTerm": "karaoke machine",
                            "clicks": 12,
                            "spend": 8.5,
                            "orders": 1,
                            "sales": 39.99,
                        }]
                    }
                }
            }
        return {}

    monkeypatch.setattr(mcp_client.MCPClient, "_rpc", fake_rpc)
    monkeypatch.setattr(mcp_client.MCPClient, "_notify", lambda *a, **k: None)
    parser = build_parser()
    args = parser.parse_args(["mcp", "suggest", "reporter", "get_report", "--args", '{"asin":"{asin}"}'])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "MCP dataSource 映射建议" in out
    assert '"rows_path": "data.rows"' in out
    assert '"Customer Search Term": "searchTerm"' in out
    assert calls[:2] == ["initialize", "tools/call"]


def test_mcp_stdio_client(tmp_path):
    from ivyea_agent.mcp_client import MCPClient

    server = tmp_path / "server.py"
    server.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    msg=json.loads(line)\n"
        "    if 'id' not in msg:\n"
        "        continue\n"
        "    method=msg.get('method')\n"
        "    if method=='initialize':\n"
        "        result={'serverInfo': {'name': 'fake'}}\n"
        "    elif method=='tools/list':\n"
        "        result={'tools': [{'name': 'echo', 'description': 'Echo'}]}\n"
        "    elif method=='tools/call':\n"
        "        result={'structuredContent': {'ok': True, 'args': msg['params']['arguments']}}\n"
        "    else:\n"
        "        result={}\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}), flush=True)\n",
        encoding="utf-8",
    )
    client = MCPClient({"transport": "stdio", "command": "python", "args": [str(server)]})
    try:
        assert client.initialize()["serverInfo"]["name"] == "fake"
        assert client.list_tools()[0]["name"] == "echo"
        assert client.call_tool("echo", {"x": 1})["structuredContent"]["args"]["x"] == 1
    finally:
        client.close()


def test_mcp_doctor(ivyea_home, capsys):
    from ivyea_agent import config, mcp_status, mcp_write
    from ivyea_agent.cli import build_parser

    config.mcp_set_server("stdio", {"transport": "stdio", "command": "python", **mcp_write.WRITE_ACTIONS_TEMPLATE})
    config.mcp_set_server("broken", {"transport": "http"})

    rows = mcp_status.status()
    assert any(r["name"] == "stdio" and r["ok"] for r in rows)
    assert any(r["name"] == "broken" and not r["ok"] for r in rows)
    assert "MCP Doctor" in mcp_status.render(rows)

    parser = build_parser()
    args = parser.parse_args(["mcp", "doctor"])
    assert args.func(args) == 1
    assert "missing_url" in capsys.readouterr().out


def test_mcp_doctor_security_hints():
    from ivyea_agent import mcp_status, mcp_write

    row = mcp_status.check_server("plain", {
        "transport": "http",
        "url": "http://mcp.example",
        "headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz"},
    })
    assert not row["ok"]
    assert "auth_over_plain_http" in row["security"]
    assert any("https" in tip for tip in row["suggestions"])

    missing = mcp_status.check_server("local", {"transport": "stdio", "command": "definitely-missing-mcp-bin"})
    assert not missing["ok"]
    assert "stdio_command_not_found" in missing["security"]

    writer = mcp_status.check_server("writer", {"transport": "http", "url": "https://mcp.example", **mcp_write.WRITE_ACTIONS_TEMPLATE})
    assert writer["ok"]
    assert any("--execute" in tip for tip in writer["suggestions"])
    rendered = mcp_status.render([row, writer])
    assert "security=auth_over_plain_http" in rendered
    assert "writeActions 已配置" in rendered


def test_ivyea_mcp_server_lists_and_calls_readonly_tools(ivyea_home):
    from ivyea_agent import mcp_server, task_runner, traces

    tools = mcp_server.list_tools()
    names = {tool["name"] for tool in tools}
    assert "ivyea_knowledge_search" in names
    assert "ivyea_code_plan" in names
    assert "ivyea_code_bundle" in names
    assert "ivyea_task_list" in names
    assert "ivyea_task_detail" in names
    assert "ivyea_task_resume" in names
    assert "ivyea_trace_list" in names
    assert "ivyea_trace_stats" in names
    assert "execute_actions" not in names

    task = task_runner.create("MCP task", steps=["inspect", "finish"])
    task_runner.start_next(task["id"])
    traces.record("mcp-session", "turn-1", "tool_call", "knowledge_search", ok=True, duration_ms=5, summary="ok")
    traces.record("mcp-session", "turn-1", "tool_call", "bad_tool", ok=False, duration_ms=7, summary="fail")

    init = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "ivyea-agent"

    listed = mcp_server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert any(tool["name"] == "ivyea_retrieval_search" for tool in listed["result"]["tools"])
    assert any(tool["name"] == "ivyea_code_bundle" for tool in listed["result"]["tools"])

    called = mcp_server.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "ivyea_knowledge_search", "arguments": {"query": "否词", "limit": 2}},
    })
    assert called["result"]["structuredContent"]["ok"] is True
    assert called["result"]["structuredContent"]["results"]
    assert called["result"]["isError"] is False

    tasks = mcp_server.handle_message({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "ivyea_task_list", "arguments": {"limit": 5}},
    })
    assert any(row["id"] == task["id"] for row in tasks["result"]["structuredContent"]["tasks"])

    detail = mcp_server.handle_message({
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "ivyea_task_detail", "arguments": {"id": task["id"]}},
    })
    assert detail["result"]["structuredContent"]["task"]["id"] == task["id"]

    resume = mcp_server.handle_message({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "ivyea_task_resume", "arguments": {"id": task["id"]}},
    })
    assert "Ivyea Task Resume" in resume["result"]["structuredContent"]["resume"]
    assert resume["result"]["structuredContent"]["next_step"]["title"] == "inspect"

    trace_rows = mcp_server.handle_message({
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": "ivyea_trace_list", "arguments": {"session_id": "mcp-session", "limit": 5}},
    })
    assert len(trace_rows["result"]["structuredContent"]["traces"]) == 2

    trace_stats = mcp_server.handle_message({
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "ivyea_trace_stats", "arguments": {"limit": 10}},
    })
    assert trace_stats["result"]["structuredContent"]["stats"]["failures"] >= 1

    missing = mcp_server.handle_message({
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "ivyea_missing", "arguments": {}},
    })
    assert missing["result"]["isError"] is True


def test_ivyea_mcp_server_stdio_loop(ivyea_home):
    from ivyea_agent import mcp_server

    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n" +
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    stdout = io.StringIO()
    assert mcp_server.serve_stdio(stdin, stdout) == 0
    rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert rows[0]["result"]["serverInfo"]["name"] == "ivyea-agent"
    assert any(tool["name"] == "ivyea_health" for tool in rows[1]["result"]["tools"])


def test_cli_mcp_self_config(ivyea_home, capsys):
    from ivyea_agent.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["mcp", "self-config"])
    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["transport"] == "stdio"
    assert data["args"] == ["mcp", "serve"]
