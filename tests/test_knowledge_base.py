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


def test_user_knowledge_import_search_audit_rebuild(ivyea_home, tmp_path):
    src = tmp_path / "prime-day.md"
    src.write_text("# Prime Day打法\n\nPrime Day 前提高预算，但保护品牌词，不误否核心词。", encoding="utf-8")

    card = knowledge.import_file(
        str(src),
        title="Prime Day Playbook",
        source_type="user",
        tags=["prime-day", "预算"],
        card_id="user.prime_day_playbook",
        license="user_supplied",
    )
    assert card["id"] == "user.prime_day_playbook"
    assert card["body_hash"]
    assert card["license"] == "user_supplied"
    assert knowledge.get_card("user.prime_day_playbook")["body"].startswith("# Prime Day")

    hits = knowledge.search("Prime Day 预算 品牌词", limit=5)
    assert any(h["id"] == "user.prime_day_playbook" for h in hits)

    audit = knowledge.render_audit()
    assert "user.prime_day_playbook | user | user" in audit
    assert "license=user_supplied" in audit

    idx = knowledge.rebuild_index()
    assert idx["cards"] >= 1
    ihits = knowledge.search_index("Prime Day", limit=5)
    assert any(h["id"] == "user.prime_day_playbook" for h in ihits)

    # 删除正文后 rebuild 应清理 sources.jsonl 中的缺失项。
    (ivyea_home / "knowledge" / card["path"]).unlink()
    res = knowledge.rebuild()
    assert res["missing_pruned"] == ["user.prime_day_playbook"]
    assert not knowledge.list_user_cards()


def test_knowledge_cli_import_and_rebuild(ivyea_home, tmp_path, capsys):
    from ivyea_agent.cli import main

    src = tmp_path / "listing-note.md"
    src.write_text("主图不清晰会影响 CTR，Listing 承接不足不要急着否词。", encoding="utf-8")

    assert main(["knowledge", "import", str(src), "--id", "user.listing_note", "--tags", "listing,ctr", "--license", "user_supplied"]) == 0
    out = capsys.readouterr().out
    assert "user.listing_note" in out

    assert main(["knowledge", "search", "主图 CTR", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "user.listing_note" in out

    assert main(["knowledge", "rebuild"]) == 0
    out = capsys.readouterr().out
    assert "已重建用户知识索引" in out
    assert "index:" in out


def test_knowledge_conflict_audit(ivyea_home, tmp_path):
    src = tmp_path / "negative.md"
    src.write_text("不建议使用 negative keywords，avoid negative targeting。", encoding="utf-8")
    knowledge.import_file(
        str(src),
        title="Negative hot take",
        source_type="user",
        tags=["negative-keywords"],
        card_id="user.negative_hot_take",
    )
    rows = knowledge.conflicts()
    assert any(r["id"] == "user.negative_hot_take" for r in rows)
    rendered = knowledge.render_conflicts()
    assert "冲突审计" in rendered
    assert "user.negative_hot_take" in rendered
