"""分词器单测：中文 bigram 召回 + 拉丁/数字标识符不被切碎。

这两条是对立的需求，一起测才有意义：切得太碎则 ASIN 串号，切得太粗则中文换个说法就抓瞎。
"""
from __future__ import annotations

from ivyea_agent import textseg


def test_cjk_bigram():
    assert textseg.tokenize("广告优化") == ["广告", "告优", "优化"]


def test_single_cjk_char_survives():
    """单字块必须退化成该字本身，否则「否」这类单字查询永远召不回。"""
    assert textseg.tokenize("否") == ["否"]


def test_latin_and_digits_kept_whole():
    """ASIN/SKU 这类标识符整词保留——切碎就会 B08XYZ123 串到 B08XYZ124。"""
    toks = textseg.tokenize("ASIN B08XYZ123 的 ACoS 是 35")
    assert "b08xyz123" in toks
    assert "acos" in toks          # 统一小写，查询 acos 能命中正文 ACoS
    assert "35" in toks
    assert "b08xyz12" not in toks  # 没有被切成前缀


def test_mixed_run_boundaries():
    """中英混排要在边界断开，不能把中文和拉丁粘成一个 token。"""
    toks = textseg.tokenize("降bid")
    assert "bid" in toks
    assert "降" in toks


def test_punctuation_dropped():
    """标点/空白不进 token：它们没有检索信息量，还会污染 FTS5 查询语法。"""
    assert textseg.tokenize("广告，优化！") == ["广告", "优化"]


def test_match_query_is_or_joined_and_quoted():
    mq = textseg.match_query("广告优化")
    assert mq == '"广告" OR "告优" OR "优化"'


def test_match_query_dedupes():
    """重复 token 在查询里只留一份，避免 OR 子句无谓膨胀。"""
    mq = textseg.match_query("广告广告广告")
    assert mq.count('"广告"') == 1


def test_match_query_empty_input():
    """纯标点/空串必须产出空查询，否则会拼出非法的 FTS5 MATCH 语法把检索打挂。"""
    assert textseg.match_query("") == ""
    assert textseg.match_query("，。！ ") == ""


def test_max_tokens_capped():
    toks = textseg.tokenize("广" * (textseg.MAX_TOKENS * 2))
    assert len(toks) <= textseg.MAX_TOKENS


def test_overlap_score_fallback():
    assert textseg.overlap_score("广告优化", "广告优化预算") > 0.9
    assert textseg.overlap_score("广告优化", "库存周转") == 0.0
    assert textseg.overlap_score("", "任意") == 0.0
