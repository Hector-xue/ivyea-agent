from __future__ import annotations


def test_listing_audit_detects_intent_gaps_and_risks():
    from ivyea_agent import listing_audit

    res = listing_audit.audit(
        title="Wireless Karaoke Microphone",
        bullets="Bluetooth speaker, long battery life",
        aplus="Party scenes and gift ideas",
        search_terms=["karaoke microphone for kids", "recording studio mic"],
        reviews="Some users say it does not work after one week",
        rating=3.8,
        review_count=8,
    )
    assert res["gaps"]
    assert any(r["area"] == "reviews" and r["level"] == "high" for r in res["risks"])
    text = listing_audit.render(res)
    assert "Listing 转化诊断" in text
    assert "recording studio mic" in text


def test_listing_cli_and_agent_tool(capsys):
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH, ToolContext
    from ivyea_agent.cli import main

    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "run_listing_audit" in names
    out = _DISPATCH["run_listing_audit"]({
        "title": "Karaoke Machine for Kids",
        "bullets": "Portable speaker with microphone",
        "search_terms": ["karaoke machine kids"],
    }, ToolContext())
    assert "广告动作建议" in out

    assert main([
        "listing", "audit",
        "--title", "Karaoke Machine",
        "--bullets", "Portable Bluetooth Speaker",
        "--search-terms", "karaoke machine kids,studio mic",
        "--rating", "4.5",
        "--review-count", "20",
    ]) == 0
    cli_out = capsys.readouterr().out
    assert "studio mic" in cli_out
