from __future__ import annotations


def test_review_audit_detects_quality_offer_risks():
    from ivyea_agent import review_audit

    res = review_audit.audit(
        reviews="Poor quality, stopped working after one week. Missing cable.",
        qa="Customer asks about warranty and refund.",
        rating=3.8,
        review_count=6,
        price=39.99,
        competitor_price=29.99,
        coupon="none",
    )
    assert any(i["type"] == "quality" for i in res["issues"])
    assert any(i["type"] == "not_working" for i in res["issues"])
    assert any(r["area"] == "price" and r["level"] == "high" for r in res["risks"])
    assert "不要激进放量" in res["ad_guidance"]
    text = review_audit.render(res)
    assert "Review / Q&A / Offer 归因" in text
    assert "quality" in text


def test_review_cli_and_agent_tool(capsys):
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH, ToolContext
    from ivyea_agent.cli import main

    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "run_review_audit" in names
    out = _DISPATCH["run_review_audit"]({
        "reviews": "not work, broken",
        "rating": 3.6,
        "review_count": 5,
    }, ToolContext())
    assert "广告动作建议" in out
    assert "not_working" in out or "quality" in out

    assert main([
        "review", "audit",
        "--reviews", "质量差，不能用，少件",
        "--rating", "3.7",
        "--review-count", "4",
        "--price", "39.99",
        "--competitor-price", "30",
    ]) == 0
    cli_out = capsys.readouterr().out
    assert "信任/报价风险" in cli_out
    assert "price" in cli_out
