from __future__ import annotations


def test_competitor_audit_detects_competitor_asin_and_missing_core():
    from ivyea_agent import competitor_audit

    res = competitor_audit.audit(
        own_terms="karaoke machine,kids microphone",
        search_terms="acme karaoke,b0abcdef12,wireless microphone",
        competitor_terms="acme",
        category_terms="microphone,karaoke",
        protected_terms="karaoke machine",
    )
    assert any(r["term"] == "acme karaoke" for r in res["competitor_hits"])
    assert any(r["term"] == "b0abcdef12" for r in res["asin_hits"])
    assert any(r["term"] == "kids microphone" for r in res["missing_core"])
    text = competitor_audit.render(res)
    assert "竞品 / 类目关键词诊断" in text
    assert "acme karaoke" in text


def test_competitor_cli_and_agent_tool(capsys):
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH, ToolContext
    from ivyea_agent.cli import main

    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "run_competitor_audit" in names
    out = _DISPATCH["run_competitor_audit"]({
        "own_terms": ["karaoke machine"],
        "search_terms": ["acme karaoke", "B0ABCDEFGH"],
        "competitor_terms": ["acme"],
    }, ToolContext())
    assert "竞品流量" in out

    assert main([
        "competitor", "audit",
        "--own-terms", "karaoke machine,kids microphone",
        "--search-terms", "acme karaoke,B0ABCDEFGH,wireless microphone",
        "--competitor-terms", "acme",
        "--category-terms", "microphone,karaoke",
    ]) == 0
    cli_out = capsys.readouterr().out
    assert "ASIN 串号词" in cli_out
    assert "缺失核心词" in cli_out
