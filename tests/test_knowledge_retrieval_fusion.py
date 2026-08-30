"""检索融合层：中文分词、BM25 长度归一化、摘录质量、预算分配、热路径守卫。"""
from __future__ import annotations

from ivyea_agent import knowledge, knowledge_quality


def test_chinese_query_is_tokenized_into_ngrams():
    """整句中文必须被切开。

    切不开的话 "广告花了钱不出单" 就是一个 token，它不可能出现在任何卡片里，
    词法检索必然零命中——这正是之前中文口语问法全军覆没的根因。
    """
    terms = knowledge._tokenize("广告花了钱不出单")
    assert "广告" in terms
    assert "出单" in terms
    # 整句本身不该是唯一的检索词
    assert len(terms) > 1


def test_tokenizer_keeps_error_codes_intact():
    """错误码/SKU 这类高区分度 token 不能被切碎，还要拿到最高权重。"""
    terms = knowledge._tokenize("上架报错 90220 parent_sku 无效")
    assert "90220" in terms
    assert "parent_sku" in terms
    assert terms["parent_sku"] >= max(terms[t] for t in terms if t in ("上架", "无效"))


def test_colloquial_operator_questions_recall_official_cards():
    """运营口语问法必须能召回官方卡，而不是只剩用户上传的长文章。"""
    for query in ("广告花了钱不出单", "库存老是不够卖"):
        evidence = knowledge.evidence_context(query, limit=4)
        assert evidence["ids"], f"{query} 零命中"
        assert any(
            not card_id.startswith("user.") for card_id in evidence["ids"]
        ), f"{query} 只召回了用户卡：{evidence['ids']}"


def test_domain_gate_covers_colloquial_but_not_coding():
    """门控要认得运营口语，又不能把亚马逊证据注进编码对话。"""
    assert knowledge.retrieval_decision("链接突然没曝光了")["should_retrieve"] is True
    assert knowledge.retrieval_decision("产品一直没有自然单")["should_retrieve"] is True
    assert knowledge.retrieval_decision("你好，帮我写 Python")["should_retrieve"] is False
    assert knowledge.retrieval_decision("帮我 code review 一下")["should_retrieve"] is False


def test_snippet_skips_card_metadata_header():
    """摘录不能落在卡片头部的元数据样板上——那些字段引证行里已经给过一遍。"""
    body = (
        "# Targeting with Sponsored Products\n\n"
        "Source type: official\n"
        "Source URL: https://example.com\n"
        "Retrieved at: 2026-07-01\n"
        "License: amazon_public_docs_summary\n"
        "Quality: authoritative\n"
        "## What the official source establishes\n"
        "Negative targeting prevents ads from showing on irrelevant queries.\n"
    )
    snippet = knowledge._snippet(body, ["不存在的词"])
    assert "Source URL" not in snippet
    assert "License" not in snippet


def test_snippet_skips_alternate_header_format():
    """第二种头部写法（Updated / Sources + URL 列表）也要跳过。"""
    body = (
        "# Sponsored Products Bidding\n"
        "Source type: official summary\n"
        "Updated: 2026-06\n"
        "Sources:\n"
        "- https://advertising.amazon.com/solutions\n"
        "Bid adjustments should stay within 20% unless overridden.\n"
    )
    snippet = knowledge._snippet(body, ["不存在的词"])
    assert "Updated:" not in snippet
    assert "https://" not in snippet


def test_snippet_prefers_densest_window():
    """命中多个词时取最密集的窗口，而不是第一个命中位置。"""
    body = "## Body\n" + ("filler " * 40) + "bid budget conversion all together here\n"
    snippet = knowledge._snippet(body, ["bid", "budget", "conversion"], width=120)
    assert "budget" in snippet and "conversion" in snippet


def test_long_user_document_does_not_crowd_out_official_cards():
    """BM25 长度归一化：超长文档不能靠"够长"把正经卡片挤出去。"""
    hits = knowledge.search("negative targeting search term", limit=5)
    assert hits
    assert any(not h["id"].startswith("user.") for h in hits)


def test_more_citations_never_shrinks_covered_content():
    """召回更多不该让模型看到更少。

    旧实现是"全部拼好、超了砍尾巴"，第 5 条把总长顶过 max_chars 时会一刀切掉
    尾部，连引证键一起切没。改成按条数分配预算后，引证条数必须跟得上 limit。
    """
    four = knowledge.evidence_context("广告花了钱不出单怎么办", limit=4)
    five = knowledge.evidence_context("广告花了钱不出单怎么办", limit=5)
    assert len(five["citations"]) >= len(four["citations"])
    assert not five["text"].rstrip().endswith("...")
    # 每条引证都得留下有论证价值的摘录，不能被压成碎片
    assert all(len(c["snippet"]) >= 80 for c in five["citations"])


def test_market_bonus_is_generic_across_marketplaces():
    """站点加分由 marketplaces 字段驱动，不是只硬编码 JP/UK。"""
    assert knowledge._query_markets("加拿大站卖家注册") == {"CA"}
    assert knowledge._query_markets("墨西哥站佣金") == {"MX"}
    assert "JP" in knowledge._query_markets("日本站危险品")
    # 两字母站点码不能按子串匹配，否则 because/point 这类词会污染整张表
    assert knowledge._query_markets("because the point is unclear") == set()


def test_vector_path_never_rebuilds_index_on_hot_path(monkeypatch):
    """注入是热路径：索引缺失时必须放弃向量路，绝不触发同步重建。"""
    from ivyea_agent import retrieval_index

    called = {"search": 0, "rebuild": 0}

    def fake_status():
        return {"enabled": True, "chunks": 0}

    def fake_search(*args, **kwargs):
        called["search"] += 1
        return []

    def fake_rebuild(*args, **kwargs):
        called["rebuild"] += 1
        return {}

    monkeypatch.setattr(retrieval_index, "status", fake_status)
    monkeypatch.setattr(retrieval_index, "search", fake_search)
    monkeypatch.setattr(retrieval_index, "rebuild", fake_rebuild)

    assert knowledge._vector_candidates("广告花了钱不出单", 8) == []
    assert called["search"] == 0
    assert called["rebuild"] == 0


def test_vector_path_degrades_on_error(monkeypatch):
    """向量路出任何问题都退回纯词法，不能让一次检索抛异常。"""
    from ivyea_agent import retrieval_index

    monkeypatch.setattr(retrieval_index, "status", lambda: {"enabled": True, "chunks": 10})

    def boom(*args, **kwargs):
        raise RuntimeError("index corrupted")

    monkeypatch.setattr(retrieval_index, "search", boom)
    assert knowledge._vector_candidates("广告", 8) == []
    hits = knowledge._fused_candidates("negative targeting", limit=5)
    assert hits  # 词法路照常出结果


def test_quality_gate_excludes_known_gaps():
    """known_gap 案例不计门禁，但必须继续出现在结果里。"""
    result = knowledge_quality.run()
    gaps = [row for row in result["results"] if row.get("known_gap")]
    assert gaps, "known_gap 案例被删了——缺口必须保持可见"
    assert result["summary"]["cases"] == len(result["results"]) - len(gaps)
    assert result["summary"]["known_gaps"] == len(gaps)


def test_quality_cases_have_answer_level_assertions():
    """案例集必须真的带上答案级断言，而不只是召回断言。"""
    cases = knowledge_quality.cases()
    assert any(case.get("golden_points") for case in cases)
    assert any(case.get("expect_guard") for case in cases)
    assert any(case.get("forbidden") for case in cases)


def test_hallucination_trap_retrieves_guardrail_card():
    """问一个亚马逊没有的机制时，护栏卡必须在证据里。"""
    evidence = knowledge.evidence_context("亚马逊 A10 算法的官方权重表是多少", limit=5)
    assert any(card_id.startswith("governance.") for card_id in evidence["ids"])
