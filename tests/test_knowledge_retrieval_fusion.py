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


def test_quality_gate_excludes_known_gaps(monkeypatch):
    """known_gap 机制：标了的案例不计门禁，但必须继续出现在结果里。

    测的是**机制**而不是"当前存在几个缺口"——缺口补完之后前者依然要成立。
    合成一个必然失败的 known_gap 案例：它不该把门禁拉红，但必须留在结果里可见。
    """
    real_cases = knowledge_quality.cases()
    synthetic = dict(real_cases[0])
    synthetic.update({
        "id": "synthetic.known_gap", "known_gap": True,
        "query": "一个知识库里肯定没有的问法 zzz",
        "expected_ids": ["definitely.not.a.real.card"], "max_rank": 1,
    })
    monkeypatch.setattr(knowledge_quality, "cases", lambda: real_cases + [synthetic])

    result = knowledge_quality.run()
    gaps = [row for row in result["results"] if row.get("known_gap")]
    assert len(gaps) == 1
    assert gaps[0]["ok"] is False               # 它确实是红的
    assert result["ok"] is True                 # 但门禁不该被它拉红
    assert result["summary"]["known_gaps"] == 1
    assert result["summary"]["cases"] == len(result["results"]) - 1
    # 红的必须留在结果里可见——删掉换绿色是假的
    assert any(row["id"] == "synthetic.known_gap" for row in result["results"])


def test_all_known_gaps_are_currently_closed():
    """当前不该有遗留缺口。新标 known_gap 是允许的，但要显式改这条断言。"""
    result = knowledge_quality.run()
    assert result["summary"]["known_gaps"] == 0, (
        "有新的 known_gap 出现，确认是真修不了再更新这条断言"
    )


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


# ---- 证据强度日志（补卡优先级的数据来源）----

def test_retrieval_log_records_evidence_strength(ivyea_home):
    """每次亚马逊域检索都要留一笔证据强度。

    补卡优先级本该按真实提问频次排，但会话历史里只有 21 条非命令提问、且基本是开发
    调试——那份数据不存在。这个日志就是去把它攒出来。
    """
    knowledge.evidence_context("广告花了钱不出单", limit=4)
    knowledge.evidence_context("亚马逊超级铂金标怎么申请", limit=4)
    result = knowledge.knowledge_gaps()
    assert result["total_events"] >= 2
    queries = {row["query"] for row in result["weakest"]}
    assert "广告花了钱不出单" in queries


def test_retrieval_log_ranks_weakest_evidence_first(ivyea_home):
    """排序按证据强度：权威卡少、词法分低的排前面。"""
    knowledge.evidence_context("有人跟卖我的链接怎么办", limit=4)      # 有真覆盖
    knowledge.evidence_context("亚马逊超级铂金标怎么申请", limit=4)     # 编造的机制
    rows = knowledge.knowledge_gaps()["weakest"]
    order = [r["query"] for r in rows]
    assert order.index("亚马逊超级铂金标怎么申请") < order.index("有人跟卖我的链接怎么办")


def test_non_amazon_query_is_not_logged(ivyea_home):
    """编码类问题不该进这份清单——它压根不是亚马逊检索。"""
    knowledge.evidence_context("编译链接报错 undefined symbol", limit=4)
    assert knowledge.knowledge_gaps()["total_events"] == 0


def test_logging_failure_never_breaks_retrieval(ivyea_home, monkeypatch):
    """记日志失败也必须把证据正常返回。"""
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(knowledge, "retrieval_log_file", boom)
    evidence = knowledge.evidence_context("广告花了钱不出单", limit=4)
    assert evidence["citations"]


def test_evidence_standard_guard_attached_for_invented_mechanism(ivyea_home):
    """问一个具名机制（某某认证/等级）时必须挂上证据标准护栏卡。

    最容易出事的不是答不出来，是顺着问题把一个不存在的机制编圆。
    """
    evidence = knowledge.evidence_context("亚马逊卖家等级 S3 认证怎么申请", limit=5)
    assert "governance.professional_knowledge_standard" in evidence["ids"]
