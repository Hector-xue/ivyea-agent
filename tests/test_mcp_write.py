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

