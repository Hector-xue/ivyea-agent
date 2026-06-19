"""Deterministic listing conversion audit.

This module connects ad/search-term signals to listing content gaps without
pretending to know product facts that were not provided.
"""
from __future__ import annotations

import re
from typing import Any


_STOP = {
    "the", "and", "for", "with", "from", "this", "that", "your", "you", "are",
    "产品", "适用", "一个", "以及", "使用", "可以", "我们", "这个",
}


def _tokens(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9+.-]*|[\u4e00-\u9fff]{2,}", text.lower())
    out = []
    seen = set()
    for t in raw:
        if t in _STOP or len(t) < 2:
            continue
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


def _coverage(terms: list[str], text: str) -> list[dict[str, Any]]:
    low = text.lower()
    rows = []
    for term in terms:
        tt = _tokens(term)
        matched = [t for t in tt if t in low]
        rows.append({
            "term": term,
            "tokens": tt,
            "matched": matched,
            "coverage": len(matched) / len(tt) if tt else 0.0,
        })
    return rows


def audit(*, title: str = "", bullets: str = "", aplus: str = "",
          search_terms: list[str] | None = None, reviews: str = "",
          price: float | None = None, rating: float | None = None,
          review_count: int | None = None) -> dict[str, Any]:
    search_terms = [s.strip() for s in (search_terms or []) if s and s.strip()]
    listing_text = "\n".join([title, bullets, aplus]).strip()
    cov = _coverage(search_terms, listing_text) if search_terms else []
    gaps = [r for r in cov if r["coverage"] < 0.5]

    risks: list[dict[str, str]] = []
    if not title.strip():
        risks.append({"area": "title", "level": "high", "reason": "未提供标题，无法承接核心搜索意图。"})
    elif len(_tokens(title)) < 5:
        risks.append({"area": "title", "level": "medium", "reason": "标题信息偏少，可能无法覆盖关键属性/场景。"})
    if not bullets.strip():
        risks.append({"area": "bullets", "level": "high", "reason": "未提供五点，转化理由和异议处理不足。"})
    if not aplus.strip():
        risks.append({"area": "a_plus", "level": "medium", "reason": "未提供 A+ 内容，品牌故事/对比/场景证明不足。"})
    if rating is not None and rating < 4.0:
        risks.append({"area": "reviews", "level": "high", "reason": f"评分 {rating:.1f} 偏低，广告放量前需处理信任问题。"})
    if review_count is not None and review_count < 15:
        risks.append({"area": "reviews", "level": "medium", "reason": f"评论数 {review_count} 偏少，新品期应保守评估 CVR。"})
    if price is not None and price <= 0:
        risks.append({"area": "offer", "level": "medium", "reason": "价格无效或未确认，无法判断 offer 竞争力。"})
    if reviews and any(k in reviews.lower() for k in ("broken", "质量差", "不耐用", "not work", "doesn't work")):
        risks.append({"area": "reviews", "level": "high", "reason": "Review 摘要出现质量/不可用问题，需先处理产品信任。"})

    tasks = []
    for g in gaps[:8]:
        tasks.append({
            "type": "intent_gap",
            "term": g["term"],
            "action": "补充到标题/五点/A+或图片文案，先确认该搜索词与产品真实功能匹配。",
        })
    if any(r["area"] in ("reviews", "offer") and r["level"] == "high" for r in risks):
        ad_guidance = "暂停激进放量；相关词先观察或小幅控 bid，优先修复 Review/Offer 信任问题。"
    elif gaps:
        ad_guidance = "相关搜索词不宜直接否定；先补 Listing 承接，再观察 CTR/CVR。"
    else:
        ad_guidance = "Listing 文本对已给搜索词覆盖尚可，可结合广告数据继续做收割/预算/出价判断。"

    return {
        "coverage": cov,
        "gaps": gaps,
        "risks": risks,
        "tasks": tasks,
        "ad_guidance": ad_guidance,
    }


def render(result: dict[str, Any]) -> str:
    lines = ["# Listing 转化诊断", "", f"- 广告动作建议：{result['ad_guidance']}", ""]
    lines.append("## 搜索意图覆盖")
    if not result["coverage"]:
        lines.append("（未提供搜索词）")
    else:
        for row in result["coverage"]:
            lines.append(f"- {row['term']}: 覆盖 {row['coverage']:.0%}，命中 {', '.join(row['matched']) or '-'}")
    lines.append("")
    lines.append("## 转化风险")
    if not result["risks"]:
        lines.append("（未发现明显文本层风险）")
    else:
        for r in result["risks"]:
            lines.append(f"- [{r['level']}] {r['area']}: {r['reason']}")
    lines.append("")
    lines.append("## Listing 任务")
    if not result["tasks"]:
        lines.append("（暂无明确补充任务）")
    else:
        for t in result["tasks"]:
            lines.append(f"- {t['term']}: {t['action']}")
    return "\n".join(lines) + "\n"
