from __future__ import annotations


def test_offer_audit_blocks_unprofitable_low_inventory_scaling():
    from ivyea_agent import offer_audit

    res = offer_audit.audit(
        price=39.99,
        competitor_price=29.99,
        margin_rate=0.30,
        target_acos=0.35,
        inventory_days=9,
        spend=120,
        sales=250,
    )
    assert any(r["area"] == "inventory" and r["level"] == "high" for r in res["risks"])
    assert any(r["area"] == "margin" and r["level"] == "high" for r in res["risks"])
    assert "控预算" in res["ad_guidance"] or "不要加预算" in res["ad_guidance"]
    text = offer_audit.render(res)
    assert "Offer / 库存 / 利润诊断" in text
    assert "当前广告 ACOS" in text


def test_offer_cli_and_agent_tool(capsys):
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH, ToolContext
    from ivyea_agent.cli import main

    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "run_offer_audit" in names
    out = _DISPATCH["run_offer_audit"]({
        "margin_rate": 0.3,
        "target_acos": 0.4,
        "inventory_days": 8,
    }, ToolContext())
    assert "广告动作建议" in out

    assert main([
        "offer", "audit",
        "--price", "39.99",
        "--competitor-price", "29.99",
        "--margin-rate", "0.3",
        "--target-acos", "0.35",
        "--inventory-days", "9",
        "--spend", "100",
        "--sales", "200",
    ]) == 0
    cli_out = capsys.readouterr().out
    assert "库存" in cli_out
    assert "盈亏 ACOS" in cli_out
