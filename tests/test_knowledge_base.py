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
    assert "governance.source_quality" in ids
    card = knowledge.get_card("amazon_ads.sponsored_products_targeting")
    assert card and "Negative targeting" in card["body"]
    assert card["freshness"] in {"current", "reviewed"}
    assert card["source_quality"] == "authoritative"


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
    assert "confidence=" in text and "freshness=" in text


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
    assert "freshness=" in rendered and "quality=" in rendered


def test_knowledge_source_governance_card_and_audit():
    hits = knowledge.search("来源 置信 时效 evidence", limit=5)
    assert any(h["id"] == "governance.source_quality" for h in hits)
    card = knowledge.get_card("amazon_ads.sponsored_products_targeting")
    assert card["license"] == "amazon_public_docs_summary"
    assert card["body_hash"]
    audit = knowledge.render_audit()
    assert "governance.source_quality" in audit
    assert "quality=synthesized_with_official_anchor" in audit
    assert "license=amazon_public_docs_summary" in audit
    data = knowledge.audit()
    assert data["summary"]["cards"] >= 1
    assert data["summary"]["builtin_cards"] >= 1
    assert data["summary"]["source_registry"]["sources"] >= 1
    assert any(row["id"] == "governance.source_quality" for row in data["cards"])
    registry = knowledge.source_registry()
    assert registry["summary"]["official_sources"] >= 1
    assert registry["summary"]["categories"]["amazon_ads"] >= 1
    assert any("amazon_ads.sponsored_products_targeting" in {c["id"] for c in row["cards"]} for row in registry["sources"])
    rendered_sources = knowledge.render_source_registry()
    assert "知识来源登记表" in rendered_sources
    assert "official" in rendered_sources


def test_knowledge_watchlist_and_reviewed_update_flow(ivyea_home, tmp_path):
    watch = knowledge.source_watchlist()
    assert watch["summary"]["sources"] >= 1
    assert watch["summary"]["official_sources"] >= 1
    assert watch["summary"]["review_required_sources"] >= 1
    assert any(row["id"] == "amazon_ads.sponsored_products" for row in watch["sources"])
    assert any(row["id"] == "zhiwubuyan" and row["review_required"] for row in watch["sources"])
    rendered_watch = knowledge.render_source_watchlist()
    assert "知识来源观察清单" in rendered_watch
    assert "manual_review_before_import" in rendered_watch

    src = tmp_path / "budget-guard.md"
    src.write_text(
        "Sponsored Products 预算复盘：先看 placement、搜索词和 Listing 承接，"
        "再决定是否加预算或降出价；不要因为短期 ACOS 波动直接否定核心词。",
        encoding="utf-8",
    )
    draft = knowledge.draft_update_from_file(
        str(src),
        title="Budget Guard",
        source_url="https://advertising.amazon.com/solutions/products/sponsored-products",
        source_type="official",
        tags="budget,placement,negative",
        card_id="user.budget_guard",
        license="amazon_public_docs_summary",
    )
    assert draft["action"] == "create"
    assert draft["card_id"] == "user.budget_guard"
    assert draft["old_hash"] == ""
    assert "+Sponsored Products" in draft["diff"]
    assert "body" in draft

    blocked = knowledge.apply_update(draft, confirm=False)
    assert blocked["ok"] is False
    assert blocked["error"] == "confirmation_required"

    applied = knowledge.apply_update(draft, confirm=True, rebuild_indexes=False)
    assert applied["ok"] is True
    assert applied["applied"] is True
    assert knowledge.get_card("user.budget_guard")["body"].startswith("Sponsored Products")

    src.write_text(
        "Sponsored Products 预算复盘：先看 placement、搜索词、库存和 Listing 承接，"
        "再决定是否加预算或降出价；不要因为短期 ACOS 波动直接否定核心词。",
        encoding="utf-8",
    )
    updated = knowledge.draft_update_from_file(
        str(src),
        title="Budget Guard",
        source_url="https://advertising.amazon.com/solutions/products/sponsored-products",
        source_type="official",
        tags=["budget", "placement", "inventory"],
        card_id="user.budget_guard",
        license="amazon_public_docs_summary",
    )
    assert updated["action"] == "update"
    assert "-Sponsored Products 预算复盘：先看 placement、搜索词和 Listing 承接" in updated["diff"]
    assert "+Sponsored Products 预算复盘：先看 placement、搜索词、库存和 Listing 承接" in updated["diff"]
    applied_update = knowledge.apply_update(updated, confirm=True, rebuild_indexes=True)
    assert applied_update["ok"] is True
    assert applied_update["indexes"]["knowledge"]["cards"] >= 1

    noop = knowledge.draft_update(
        "Budget Guard",
        knowledge.get_card("user.budget_guard")["body"],
        source_url="https://advertising.amazon.com/solutions/products/sponsored-products",
        source_type="official",
        card_id="user.budget_guard",
    )
    assert noop["action"] == "noop"
    assert "无内容变更" in knowledge.render_update_draft(noop)


def test_knowledge_upload_folder_and_apply_flow(ivyea_home):
    payload = (
        "# 上传的广告 SOP\n\n"
        "Sponsored Products 搜索词复盘要先看 CTR、CVR、库存和 Listing 承接，再决定否词或放量。"
    ).encode("utf-8")
    uploaded = knowledge.upload_document(
        "ad-sop.md",
        payload,
        title="上传广告 SOP",
        source_type="user",
        tags=["ads", "sop"],
        card_id="user.uploaded_ad_sop",
        confirm=False,
    )
    assert uploaded["ok"] is True
    assert uploaded["upload"]["raw_path"].startswith("uploads/")
    assert uploaded["upload"]["extracted_path"].endswith("extracted.md")
    assert uploaded["draft"]["action"] == "create"
    assert knowledge.get_card("user.uploaded_ad_sop") is None

    files = knowledge.list_files()
    assert any(row["kind"] == "raw" and row["path"] == uploaded["upload"]["raw_path"] for row in files["uploads"])
    assert any(row["kind"] == "extracted" and row["path"] == uploaded["upload"]["extracted_path"] for row in files["uploads"])

    read = knowledge.read_file(uploaded["upload"]["extracted_path"])
    assert "搜索词复盘" in read["content"]

    blocked = knowledge.apply_upload(uploaded["upload"]["id"], confirm=False)
    assert blocked["ok"] is False
    assert blocked["result"]["error"] == "confirmation_required"

    applied = knowledge.apply_upload(uploaded["upload"]["id"], confirm=True, rebuild_indexes=False)
    assert applied["ok"] is True
    assert applied["result"]["applied"] is True
    card = knowledge.get_card("user.uploaded_ad_sop")
    assert card and "Sponsored Products" in card["body"]

    removed = knowledge.delete_file(card["path"])
    assert removed["ok"] is True
    assert removed["removed_card_ids"] == ["user.uploaded_ad_sop"]
    assert knowledge.get_card("user.uploaded_ad_sop") is None


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
    structured = knowledge.audit()
    assert structured["summary"]["user_cards"] == 1
    assert any(row["id"] == "user.prime_day_playbook" for row in structured["cards"])

    idx = knowledge.rebuild_index()
    assert idx["cards"] >= 1
    assert idx["source_registry"]["sources"] >= 1
    ihits = knowledge.search_index("Prime Day", limit=5)
    user_hit = next(h for h in ihits if h["id"] == "user.prime_day_playbook")
    assert user_hit["freshness"] == "current"
    assert user_hit["source_quality"] == "account_local_overrides_generic_knowledge"

    # 删除正文后 rebuild 应清理 sources.jsonl 中的缺失项。
    (ivyea_home / "knowledge" / card["path"]).unlink()
    res = knowledge.rebuild()
    assert res["missing_pruned"] == ["user.prime_day_playbook"]
    assert not knowledge.list_user_cards()


def test_import_legacy_gbrain_directory(ivyea_home, tmp_path):
    root = tmp_path / "brain"
    (root / "amazon" / "ads").mkdir(parents=True)
    (root / "amazon" / "ads" / "广告优化方法论.md").write_text(
        "# 广告优化方法论\n\n搜索词复盘先看 CTR、CVR、库存和 Listing 承接，再决定否词或放量。",
        encoding="utf-8",
    )
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("ignored", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00\x01")

    scanned = knowledge.import_directory(str(root), namespace="gbrain", confirm=False)
    assert scanned["ok"] is True
    assert scanned["summary"]["candidate_files"] == 1
    assert scanned["summary"]["imported"] == 0
    assert scanned["candidates"][0]["source_path"] == "amazon/ads/广告优化方法论.md"
    assert scanned["candidates"][0]["target_path"].startswith("user/imported/gbrain/")
    assert not knowledge.list_user_cards()

    imported = knowledge.import_directory(str(root), namespace="gbrain", confirm=True, rebuild_indexes=True)
    assert imported["summary"]["imported"] == 1
    row = imported["imported"][0]
    assert row["card"]["source_type"] == "legacy_gbrain"
    assert row["card"]["source_url"] == "gbrain://amazon/ads/广告优化方法论.md"
    assert row["card"]["path"].startswith("user/imported/gbrain/")
    card = knowledge.get_card(row["card_id"])
    assert card and "搜索词复盘" in card["body"]

    hits = knowledge.search("搜索词复盘 Listing 承接", limit=100)
    assert any(h["id"] == row["card_id"] for h in hits)
    ihits = knowledge.search_index("Listing CTR CVR", limit=20)
    assert any(h["id"] == row["card_id"] for h in ihits)

    scanned_again = knowledge.import_directory(str(root), namespace="gbrain", confirm=False)
    assert scanned_again["summary"]["noop"] == 1


def test_knowledge_cli_import_and_rebuild(ivyea_home, tmp_path, capsys):
    from ivyea_agent.cli import main

    src = tmp_path / "listing-note.md"
    src.write_text("主图不清晰会影响 CTR，Listing 承接不足不要急着否词。", encoding="utf-8")

    assert main(["knowledge", "import", str(src), "--id", "user.listing_note", "--tags", "listing,ctr", "--license", "user_supplied"]) == 0
    out = capsys.readouterr().out
    assert "user.listing_note" in out

    assert main(["knowledge", "search", "主图 CTR", "--limit", "50"]) == 0
    out = capsys.readouterr().out
    assert "user.listing_note" in out

    assert main(["knowledge", "rebuild"]) == 0
    out = capsys.readouterr().out
    assert "已重建用户知识索引" in out
    assert "index:" in out

    assert main(["knowledge", "sources"]) == 0
    out = capsys.readouterr().out
    assert "知识来源登记表" in out
    assert "user.listing_note" in out

    assert main(["knowledge", "watchlist"]) == 0
    out = capsys.readouterr().out
    assert "知识来源观察清单" in out

    update = tmp_path / "listing-update.md"
    update.write_text("Listing 更新草案：主图影响 CTR，A+ 和五点会影响 CVR，广告动作必须先看承接。", encoding="utf-8")
    assert main([
        "knowledge", "plan", str(update),
        "--id", "user.listing_update",
        "--source-url", "https://sell.amazon.com/tools/a-content",
        "--source-type", "official",
        "--tags", "listing,ctr,cvr",
        "--license", "amazon_public_docs_summary",
    ]) == 0
    out = capsys.readouterr().out
    assert "知识更新草案" in out
    assert "user.listing_update" in out

    assert main([
        "knowledge", "apply", str(update),
        "--id", "user.listing_update",
        "--source-url", "https://sell.amazon.com/tools/a-content",
        "--source-type", "official",
    ]) == 2
    out = capsys.readouterr().out
    assert "confirmation_required" in out

    assert main([
        "knowledge", "apply", str(update),
        "--id", "user.listing_update",
        "--source-url", "https://sell.amazon.com/tools/a-content",
        "--source-type", "official",
        "--confirm",
        "--no-rebuild",
    ]) == 0
    out = capsys.readouterr().out
    assert "已应用知识更新" in out
    assert knowledge.get_card("user.listing_update")


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



def test_expanded_official_amazon_knowledge_base():
    required_ids = {
        "amazon_ads.match_types",
        "amazon_ads.negative_targeting",
        "seller_central.listing_quality_guidelines",
        "fba.fulfillment_by_amazon_overview",
        "policies.intellectual_property_policy",
        "seller_university.learn_about_seller_university",
    }
    cards = {c["id"]: c for c in knowledge.list_cards()}
    assert required_ids.issubset(cards)

    for card_id in required_ids:
        card = knowledge.get_card(card_id)
        assert card["source_type"] == "official"
        assert card["license"] == "amazon_public_docs_summary"
        assert card["source_quality"] == "authoritative"
        assert card.get("source_url")
        assert "Evidence required before action" in card["body"]
        assert "Guardrails" in card["body"]

    query_expectations = [
        ("negative exact phrase negative targeting", "amazon_ads.negative_targeting"),
        ("Seller University create product listing", "seller_university.create_product_listings"),
        ("FBA inventory advertising scale stockout", "fba.inventory_management"),
        ("intellectual property competitor brand listing", "policies.intellectual_property_policy"),
        ("Manage Your Experiments listing conversion", "seller_central.manage_your_experiments"),
    ]
    for query, expected_id in query_expectations:
        hits = knowledge.search(query, limit=10)
        assert any(h["id"] == expected_id for h in hits), (query, [h["id"] for h in hits])

    registry = knowledge.source_registry()
    categories = registry["summary"]["categories"]
    assert categories["seller_university"] >= 1
    assert categories["fba"] >= 1
    assert categories["policies"] >= 1
