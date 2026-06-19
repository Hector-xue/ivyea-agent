"""Bundled Amazon knowledge base."""
from __future__ import annotations

from ivyea_agent import knowledge


def test_list_and_get_cards():
    cards = knowledge.list_cards()
    ids = {c["id"] for c in cards}
    assert "amazon_ads.sponsored_products_targeting" in ids
    assert "playbook.search_term_lifecycle" in ids
    assert "playbook.launch_maturity_strategy" in ids
    assert "playbook.report_driven_optimization" in ids
    assert "playbook.content_conversion_assets" in ids
    card = knowledge.get_card("amazon_ads.sponsored_products_targeting")
    assert card and "Negative targeting" in card["body"]


def test_search_knowledge():
    hits = knowledge.search("否词 negative targeting clicks", limit=3)
    assert hits
    assert any(h["id"].startswith("amazon_ads.") for h in hits)
    assert "snippet" in hits[0]


def test_search_knowledge_chinese_alias():
    hits = knowledge.search("否词", limit=3)
    assert hits
    assert any("negative" in " ".join(h.get("tags", [])).lower() or "negative" in h["snippet"].lower() for h in hits)


def test_search_playbooks():
    hits = knowledge.search("赢家词 收割 listing 转化", limit=5)
    ids = {h["id"] for h in hits}
    assert "playbook.search_term_lifecycle" in ids or "playbook.listing_ad_feedback_loop" in ids


def test_context_for_query_compact():
    text, ids = knowledge.context_for_query("高点击零单要不要否词", limit=2, max_chars=400)
    assert text and ids
    assert len(text) <= 404
    assert any("negative" in i or "playbook" in i for i in ids)


def test_agent_knowledge_tool_registered():
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "knowledge_search" in names
    assert "knowledge_search" in _DISPATCH


def test_new_playbooks_chinese_queries():
    hits = knowledge.search("新品 自动广告 成熟期 ACOS", limit=5)
    assert any(h["id"] == "playbook.launch_maturity_strategy" for h in hits)

    hits = knowledge.search("报表 placement 搜索词 周期", limit=5)
    assert any(h["id"] == "playbook.report_driven_optimization" for h in hits)

    rendered = knowledge.render_search("素材 A+ 转化 listing", limit=5)
    assert "playbook.content_conversion_assets" in rendered
    assert "https://sell.amazon.com/tools/a-content" in rendered
