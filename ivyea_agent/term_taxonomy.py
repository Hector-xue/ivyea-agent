"""搜索词分类与「不能否」护栏。

为什么单独一个模块、而不是复用 `rule_engine_scripts/analyze_search_term_decisions.py`：
那个脚本是 vendor 进来的上游代码，一来 pandas 耦合（生产广告引擎这条路不该为了
判断几个字符串就拖进 pandas），二来它硬编码了 `karaoke`/`videoke` 这类**特定类目**
的词表——把它整体搬进通用引擎，等于把一个店铺的调参烙进所有店铺。这里只沿用它
那套纯字符串判定思路（分词 → 归一 → 编辑距离相似 → 集合匹配），不 import 它。

护栏的风险是**不对称**的：
拦错一个该否的词，代价是这一轮少省一点无效花费；放过一个品牌词，代价是把自己的
品牌搜索流量掐掉、直接掉销量。所以拿不准的时候一律**不否**，并说明拿不准在哪。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

#: 亚马逊 ASIN 形态。买家直接搜竞品 ASIN 找到你，通常是有价值的流量，不该无脑否。
ASIN_PATTERN = re.compile(r"\bB0[A-Z0-9]{8}\b", re.IGNORECASE)

#: 常见英文虚词/泛词，推断竞品品牌时要排掉，否则 "best"、"with" 会被当成品牌。
_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "your", "that", "this", "of", "to", "in", "on",
    "amazon", "best", "top", "buy", "near", "me", "new", "cheap", "sale", "deal", "price", "review",
    "reviews", "vs", "size", "small", "large", "big", "mini", "pro", "plus", "max", "set", "kit",
    "pack", "case", "cover", "black", "white", "blue", "red", "green", "pink", "gray", "grey",
    "men", "women", "kids", "baby", "girls", "boys", "adult", "home", "car", "office",
})

#: 两个 token 相似到这个程度就算同一个词（拼写变体、单复数、连写）。
#: 沿用 vendored 脚本 brand_similarity_threshold 的取值。
DEFAULT_SIMILARITY = 0.82

#: CTR 高到账户均值的这个倍数，就认定"流量是相关的、问题在承接"。
#: 取 1.5 是**刻意偏保守**（宁可少否）：方法论把 CTR/CVR 杠杆排在否词前面，
#: 这种词的正确处理是去改 Listing，否掉只是把问题藏起来、连相关流量一起掐掉。
LISTING_ISSUE_CTR_MULTIPLE = 1.5


@dataclass
class TermContext:
    """判定所需的店铺侧上下文。全部可选——缺什么就少判什么，并如实报告。"""

    brand_tokens: set[str] = field(default_factory=set)
    competitor_tokens: set[str] = field(default_factory=set)
    strategic_tokens: set[str] = field(default_factory=set)
    #: 推断出来的疑似竞品词。和 competitor_tokens 分开放：配置来的是**权威**、
    #: 直接拦；推断来的只出警告——推断噪音大，让它硬拦会把否词杠杆废掉。
    inferred_competitor_tokens: set[str] = field(default_factory=set)
    account_cvr: float | None = None
    account_ctr: float | None = None
    #: 单笔订单的毛利额。用来判"这个词已经花掉的钱超没超过一单能赚的钱"。
    profit_per_order: float | None = None
    similarity_threshold: float = DEFAULT_SIMILARITY


def tokenize(text: Any) -> list[str]:
    if text is None:
        return []
    return re.findall(r"[a-z0-9]+", str(text).lower())


def _similar(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def matches_any(token: str, candidates: Iterable[str], threshold: float = DEFAULT_SIMILARITY) -> bool:
    """token 命中候选集。长词才做模糊匹配——短词做模糊匹配会满地误伤。"""
    for candidate in candidates:
        if token == candidate:
            return True
        if len(token) >= 4 and len(candidate) >= 4 and _similar(token, candidate) >= threshold:
            return True
    return False


def is_asin_like(term: str) -> bool:
    return bool(ASIN_PATTERN.search(str(term or "").upper()))


def hits_vocabulary(term: str, vocabulary: Iterable[str], threshold: float = DEFAULT_SIMILARITY) -> bool:
    """词表命中判定，**单词和多词短语都要认**。

    只做单 token 匹配是不够的：真实配置里"anker soundcore"、"karaoke machine" 这类
    多词品牌/战略词很常见，而它们永远不会等于任何一个单 token——只按 token 匹配
    等于这些配置全部静默失效，用户还以为护栏开着。
    """
    tokens = set(tokenize(term))
    normalized_term = " ".join(tokenize(term))
    for entry in vocabulary:
        entry_tokens = tokenize(entry)
        if not entry_tokens:
            continue
        if len(entry_tokens) > 1:
            if " ".join(entry_tokens) in normalized_term:
                return True
            continue
        if any(matches_any(token, {entry_tokens[0]}, threshold) for token in tokens):
            return True
    return False


def classify(term: str, ctx: TermContext) -> str:
    """返回 asin_term / brand_term / competitor_term / strategic_term / generic_term。

    顺序有意为之：品牌 > 战略 > 竞品 > ASIN。自家品牌词最不能碰，先判掉。
    """
    if not tokenize(term):
        return "generic_term"
    threshold = ctx.similarity_threshold
    if ctx.brand_tokens and hits_vocabulary(term, ctx.brand_tokens, threshold):
        return "brand_term"
    if ctx.strategic_tokens and hits_vocabulary(term, ctx.strategic_tokens, threshold):
        return "strategic_term"
    if ctx.competitor_tokens and hits_vocabulary(term, ctx.competitor_tokens, threshold):
        return "competitor_term"
    if is_asin_like(term):
        return "asin_term"
    return "generic_term"


#: 推断竞品词时，出现在超过这个比例的搜索词里就判定为**类目词**而非品牌词。
#: 没有这道上限，"karaoke"、"machine" 这种满屏都是的品类词会被当成竞品品牌，
#: 于是几乎每条候选都挂上"疑似竞品词"——那种到处响的警告只会训练人忽略警告，
#: 比不出警告更糟。
_CATEGORY_VOCAB_RATIO = 0.15

#: 样本小于这个量就不推断。
#: 8 个词的时候每个词就占 12.5%，比例判据分辨不出品牌词和品类词——那不叫推断，
#: 叫掷骰子。真实窗口里搜索词是几百条量级，比例才有意义。
_MIN_TERMS_FOR_INFERENCE = 30


def infer_competitor_tokens(terms: Iterable[str], ctx: TermContext,
                            min_frequency: int = 2) -> set[str]:
    """从本窗口的搜索词里推断疑似竞品品牌 token。

    纯启发式，噪音大，所以调用方只拿它出警告、不拿它硬拦。判据：字母词、长度≥4、
    不是虚词/泛词、不匹配自家品牌、在多个搜索词里反复出现（品牌会跟着不同品类词
    反复出现），**且不是满屏都有的品类词**。
    """
    unique_terms = [t for t in dict.fromkeys(str(t or "") for t in terms) if t]
    if len(unique_terms) < _MIN_TERMS_FOR_INFERENCE:
        return set()
    counts: dict[str, int] = {}
    for term in unique_terms:
        for token in set(tokenize(term)):
            if len(token) < 4 or token.isdigit() or token in _STOP_WORDS:
                continue
            if ctx.brand_tokens and hits_vocabulary(token, ctx.brand_tokens, ctx.similarity_threshold):
                continue
            counts[token] = counts.get(token, 0) + 1
    total = len(unique_terms)
    return {
        token for token, n in counts.items()
        if n >= min_frequency and (n / total) < _CATEGORY_VOCAB_RATIO
    }


def _no_order_probability(clicks: int, cvr: float) -> float:
    """按账户平均转化率，这个词拿到 0 单的概率有多大。

    用来回答"数据够不够"：账户平均 CVR 2% 时，15 次点击拿 0 单的概率约 74%——
    那根本不能证明这个词比别的词差，只能证明样本太小。
    """
    if clicks <= 0 or cvr <= 0:
        return 1.0
    return float((1.0 - min(cvr, 0.99)) ** clicks)


def clicks_for_confidence(cvr: float, confidence: float = 0.95) -> int:
    """要多少次点击，0 单才算真的说明问题。"""
    if cvr <= 0 or cvr >= 1:
        return 0
    return int(math.ceil(math.log(1.0 - confidence) / math.log(1.0 - cvr)))


def negation_guard(term: str, metrics: dict[str, Any], ctx: TermContext) -> dict[str, Any]:
    """否词护栏。返回 {allowed, reason, category, warnings}。

    实现方法论里的「不能否」清单。**硬拦**只用于能确定判定的类别；判不了的一律
    放行但出警告——把不确定藏起来比拦错更糟，人工复核看不到就等于没有护栏。
    """
    category = classify(term, ctx)
    warnings: list[str] = []
    clicks = int(metrics.get("clicks") or 0)
    orders = int(metrics.get("orders") or 0)
    spend = float(metrics.get("spend") or 0.0)
    impressions = int(metrics.get("impressions") or 0)

    # ---- 硬拦：类别确定的「不能否」----
    if category == "brand_term":
        return {"allowed": False, "reason": "品牌词：自家品牌搜索不能否（品牌流量常是比价/复购前浏览，当期 0 单不代表无效）",
                "category": category, "warnings": warnings}
    if category == "strategic_term":
        return {"allowed": False, "reason": "战略大词：已标记为战略词，不否",
                "category": category, "warnings": warnings}
    if category == "competitor_term":
        return {"allowed": False, "reason": "竞品词：竞品词承担拦截/对比价值，不否（要控成本走降 bid）",
                "category": category, "warnings": warnings}

    # ---- 硬拦：高转化词。到不了否词分支的（否词要求 0 单），留作防御 ----
    if orders > 0:
        return {"allowed": False, "reason": f"该词有 {orders} 单转化，不属于无效流量",
                "category": category, "warnings": warnings}

    # ---- 硬拦：疑似 Listing 承接问题，而非流量不相关 ----
    # CTR 明显高于账户均值 = 买家看了觉得对口才点进来，流量是相关的；0 单更可能是
    # 价格/主图/评论/详情承接不住。这种词否掉是把问题藏起来，该走 Listing 反馈。
    if ctx.account_ctr and impressions > 0 and clicks > 0:
        term_ctr = clicks / impressions
        if term_ctr >= ctx.account_ctr * LISTING_ISSUE_CTR_MULTIPLE:
            return {
                "allowed": False,
                "category": category,
                "reason": (f"疑似 Listing 承接问题：该词 CTR {term_ctr:.2%} 是账户均值 "
                           f"{ctx.account_ctr:.2%} 的 {term_ctr / ctx.account_ctr:.1f} 倍，"
                           f"流量相关性没问题，0 单更可能是价格/主图/评论承接不住 → 走 Listing 反馈"),
                "warnings": warnings,
            }

    # ---- 放行，但把不确定说清楚 ----
    if not ctx.brand_tokens:
        warnings.append("未配置品牌词，无法排除品牌词误否（设置 lingxing_brand_tokens 后此项才生效）")
    if ctx.inferred_competitor_tokens and any(
        matches_any(t, ctx.inferred_competitor_tokens, ctx.similarity_threshold)
        for t in set(tokenize(term))
    ):
        warnings.append("疑似竞品词（推断，非配置）：确认后再否，或改走降 bid")
    if category == "asin_term":
        warnings.append("ASIN 串号词：买家直接搜竞品 ASIN 找到你，通常是有价值流量，建议人工确认")

    if ctx.account_cvr:
        p_zero = _no_order_probability(clicks, ctx.account_cvr)
        if p_zero > 0.05:
            need = clicks_for_confidence(ctx.account_cvr)
            warnings.append(
                f"统计上还不够：按账户均值 CVR {ctx.account_cvr:.2%}，{clicks} 次点击拿 0 单的概率有 "
                f"{p_zero:.0%}，要 {need} 次点击才谈得上显著。当前依据是花费而非转化差异"
            )
    if ctx.profit_per_order and spend < ctx.profit_per_order:
        warnings.append(
            f"经济上还不够：该词累计花费 {spend:.2f} 尚未超过单笔毛利 {ctx.profit_per_order:.2f}，"
            f"再等一等的成本可控"
        )

    return {"allowed": True, "reason": "", "category": category, "warnings": warnings}


#: 当前数据面拿不到、因此**没有**覆盖的护栏，如实写进报告，别让人以为全覆盖了。
UNCOVERED_GUARDS = (
    "新品期差词：需要商品上架时间，领星广告数据面当前取不到，本轮未覆盖",
)
