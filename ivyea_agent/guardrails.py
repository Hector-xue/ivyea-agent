"""硬护栏 —— 执行前的安全闸（违反即阻断，不交给 LLM 判断）。

依据用户方法论铁律：
- 不否品牌词/竞品词/核心品类词/保护词/同义词
- 单次调 bid ≤20%
- 小类目核心词不降 bid
- 低置信度不自动执行
"""
from __future__ import annotations

from typing import Iterable

from .actions import Action

MAX_BID_CHANGE = 0.20  # 单次 ≤20%
NO_NEGATE_CATEGORIES = {"brand_term", "competitor_term", "core_category_term", "asin_term"}


def annotate(actions: Iterable[Action], protected_terms: Iterable[str] = ()) -> list[Action]:
    """对每个动作打护栏标记（blocked / block_reason）。返回同一列表。"""
    protected = {p.strip().lower() for p in protected_terms if p.strip()}
    result = []
    for a in actions:
        a.blocked, a.block_reason = _check(a, protected)
        result.append(a)
    return result


def _check(a: Action, protected: set[str]) -> tuple[bool, str]:
    term_l = a.search_term.lower()
    cat = a.term_category

    # 保护词：任何动作都不动（否词尤其）
    if term_l in protected:
        return True, "命中保护词清单，禁止自动处理"

    if a.kind == "negative":
        if cat in NO_NEGATE_CATEGORIES:
            return True, f"{cat} 不否（品牌/竞品/核心品类/ASIN词需人工）"
        if a.confidence == "low":
            return True, "置信度低，否词需人工复核"
        return False, ""

    # bid 类
    if a.change_pct is not None and abs(a.change_pct) > MAX_BID_CHANGE + 1e-9:
        return True, f"单次调 bid 幅度 {a.change_pct:+.0%} 超过 ±20% 上限"
    if a.kind == "reduce_bid" and cat == "core_category_term":
        return True, "小类目核心词不降 bid（应走 CTR/CVR 杠杆，见方法论）"
    if a.confidence == "low":
        return True, "置信度低，调 bid 需人工复核"
    return False, ""
