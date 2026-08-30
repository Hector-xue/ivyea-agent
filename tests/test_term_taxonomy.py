"""否词护栏：「不能否」清单的判定。"""
from __future__ import annotations

from ivyea_agent import term_taxonomy as tt


def _ctx(**kw):
    return tt.TermContext(**kw)


def _metrics(clicks=20, orders=0, spend=30.0, impressions=400):
    return {"clicks": clicks, "orders": orders, "spend": spend, "impressions": impressions}


# ---- 硬拦：类别确定的「不能否」 ----

def test_brand_term_is_never_negated():
    """自家品牌词是最贵的误伤——否掉直接掐掉品牌搜索流量。"""
    ctx = _ctx(brand_tokens={"ivyea"})
    verdict = tt.negation_guard("ivyea karaoke machine", _metrics(), ctx)
    assert verdict["allowed"] is False
    assert verdict["category"] == "brand_term"
    assert "品牌词" in verdict["reason"]


def test_brand_match_tolerates_spelling_variants():
    """买家把品牌拼错照样是品牌搜索。"""
    ctx = _ctx(brand_tokens={"ivyea"})
    assert tt.classify("ivyeah speaker", ctx) == "brand_term"


def test_short_tokens_do_not_fuzzy_match():
    """短词不做模糊匹配，否则满地误伤。"""
    ctx = _ctx(brand_tokens={"abc"})
    assert tt.classify("abd speaker", ctx) == "generic_term"


def test_configured_competitor_term_is_blocked():
    ctx = _ctx(competitor_tokens={"soundcore"})
    verdict = tt.negation_guard("soundcore speaker", _metrics(), ctx)
    assert verdict["allowed"] is False
    assert verdict["category"] == "competitor_term"


def test_strategic_term_is_blocked():
    ctx = _ctx(strategic_tokens={"karaoke"})
    verdict = tt.negation_guard("karaoke machine", _metrics(), ctx)
    assert verdict["allowed"] is False
    assert verdict["category"] == "strategic_term"


def test_converting_term_is_blocked():
    """有转化的词不该走到否词分支；留作防御。"""
    verdict = tt.negation_guard("wireless mic", _metrics(orders=3), _ctx())
    assert verdict["allowed"] is False
    assert "转化" in verdict["reason"]


def test_high_ctr_zero_order_routes_to_listing_feedback():
    """CTR 远高于账户均值 = 流量是相关的，0 单是承接问题，不该否。"""
    ctx = _ctx(account_ctr=0.005)
    verdict = tt.negation_guard(
        "portable karaoke", _metrics(clicks=40, impressions=400), ctx,
    )
    assert verdict["allowed"] is False
    assert "Listing" in verdict["reason"]


# ---- 放行 + 警告：判不了的不能假装安全 ----

def test_generic_waste_term_is_allowed():
    ctx = _ctx(brand_tokens={"ivyea"}, account_ctr=0.02, account_cvr=0.10)
    verdict = tt.negation_guard(
        "free music download", _metrics(clicks=40, impressions=4000), ctx,
    )
    assert verdict["allowed"] is True
    assert verdict["category"] == "generic_term"


def test_missing_brand_config_warns_instead_of_silently_passing():
    """没配品牌词时照样放行，但必须把"排不掉品牌词"这件事写出来。"""
    verdict = tt.negation_guard("some term", _metrics(), _ctx())
    assert verdict["allowed"] is True
    assert any("未配置品牌词" in w for w in verdict["warnings"])


def test_inferred_competitor_only_warns():
    """推断出来的竞品词噪音大，只警告不硬拦——硬拦会把否词杠杆废掉。"""
    ctx = _ctx(brand_tokens={"ivyea"}, inferred_competitor_tokens={"soundcore"})
    verdict = tt.negation_guard("soundcore mic", _metrics(), ctx)
    assert verdict["allowed"] is True
    assert any("疑似竞品词" in w for w in verdict["warnings"])


def test_asin_term_warns_for_manual_review():
    verdict = tt.negation_guard("B0ABCDEFGH case", _metrics(), _ctx(brand_tokens={"x"}))
    assert verdict["allowed"] is True
    assert verdict["category"] == "asin_term"
    assert any("ASIN" in w for w in verdict["warnings"])


def test_statistically_thin_evidence_is_flagged():
    """账户 CVR 2% 时，15 次点击 0 单说明不了问题——要说清楚依据是花费不是转化差异。"""
    ctx = _ctx(brand_tokens={"ivyea"}, account_cvr=0.02)
    verdict = tt.negation_guard("some term", _metrics(clicks=15, impressions=3000), ctx)
    assert verdict["allowed"] is True
    assert any("统计上还不够" in w for w in verdict["warnings"])


def test_enough_clicks_clears_the_statistical_warning():
    ctx = _ctx(brand_tokens={"ivyea"}, account_cvr=0.10)
    verdict = tt.negation_guard("some term", _metrics(clicks=60, impressions=6000), ctx)
    assert not any("统计上还不够" in w for w in verdict["warnings"])


def test_spend_below_unit_profit_is_flagged():
    ctx = _ctx(brand_tokens={"ivyea"}, profit_per_order=50.0)
    verdict = tt.negation_guard("some term", _metrics(spend=12.0), ctx)
    assert any("经济上还不够" in w for w in verdict["warnings"])


def test_clicks_for_confidence_matches_the_binomial():
    """95% 置信下，CVR 越低需要的点击越多。"""
    assert tt.clicks_for_confidence(0.10) == 29
    assert tt.clicks_for_confidence(0.02) > tt.clicks_for_confidence(0.10)


# ---- 竞品词推断 ----

def _term_corpus() -> list[str]:
    """一份接近真实体量的搜索词集（比例判据要有足够样本才有意义）。"""
    filler = [f"karaoke machine model {i}" for i in range(28)]
    return ["ivyea karaoke machine", "soundcore karaoke", "soundcore speaker",
            "best karaoke machine", "cheap karaoke"] + filler


def test_infer_competitor_tokens_skips_stopwords_and_own_brand():
    ctx = _ctx(brand_tokens={"ivyea"})
    inferred = tt.infer_competitor_tokens(_term_corpus(), ctx)
    assert "soundcore" in inferred
    assert "ivyea" not in inferred       # 自家品牌
    assert "best" not in inferred        # 泛词
    assert "cheap" not in inferred


def test_uncovered_guards_are_declared():
    """没覆盖的护栏必须留有明文交代，不能让人以为全覆盖。"""
    assert any("新品期" in item for item in tt.UNCOVERED_GUARDS)


# ---- 多词短语配置 ----

def test_multiword_brand_config_is_matched():
    """真实配置里"anker soundcore"这类多词品牌很常见。

    只按单 token 匹配的话，这类配置永远不会命中——护栏静默失效，用户还以为开着。
    """
    ctx = _ctx(brand_tokens={"ivyea audio"})
    assert tt.classify("ivyea audio karaoke", ctx) == "brand_term"
    verdict = tt.negation_guard("buy ivyea audio mic", _metrics(), ctx)
    assert verdict["allowed"] is False


def test_multiword_strategic_phrase_is_matched():
    ctx = _ctx(strategic_tokens={"karaoke machine"})
    assert tt.classify("karaoke machine for kids", ctx) == "strategic_term"
    # 只命中其中一个词不算：短语要连着出现
    assert tt.classify("washing machine", ctx) == "generic_term"


# ---- 竞品推断的噪音上限 ----

def test_inference_skips_category_vocabulary():
    """品类词满屏都是，不能当竞品品牌。

    没有这道上限，几乎每条候选都会挂上"疑似竞品词"，警告就没人看了。
    """
    ctx = _ctx(brand_tokens={"ivyea"})
    inferred = tt.infer_competitor_tokens(_term_corpus(), ctx)
    assert "karaoke" not in inferred      # 满屏都是 = 品类词，不是品牌
    assert "machine" not in inferred      # 同上
    assert "soundcore" in inferred        # 少数词里反复出现 = 品牌形态


def test_inference_needs_enough_terms():
    """样本太少时比例没有意义，不推断，免得瞎报。"""
    ctx = _ctx()
    # 8 个词的时候每个词就占 12.5%，比例判据分辨不出品牌词和品类词
    assert tt.infer_competitor_tokens(["soundcore mic", "soundcore speaker"], ctx) == set()
    assert tt.infer_competitor_tokens(_term_corpus()[:10], ctx) == set()
