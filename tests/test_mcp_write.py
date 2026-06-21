"""MCP writeActions templates and validation."""
from __future__ import annotations


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
