"""Competitor and category keyword audit for Amazon ad operations."""
from __future__ import annotations

import re
from typing import Any


def _split(values: str | list[str] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw = re.split(r"[,;\n]+", values)
    else:
        raw = values
    out = []
    seen = set()
    for value in raw:
        item = str(value or "").strip()
        key = item.lower()
        if item and key not in seen:
            out.append(item)
            seen.add(key)
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _contains_any(term: str, candidates: list[str]) -> list[str]:
    low = _norm(term)
    hits = []
    for c in candidates:
        cn = _norm(c)
        if cn and (cn in low or low in cn):
            hits.append(c)
    return hits


def _asin_like(term: str) -> bool:
    return bool(re.search(r"\bB0[A-Z0-9]{8}\b", term.upper()))


def audit(*, own_terms: str | list[str] | None = None,
          search_terms: str | list[str] | None = None,
          competitor_terms: str | list[str] | None = None,
          category_terms: str | list[str] | None = None,
          protected_terms: str | list[str] | None = None) -> dict[str, Any]:
    own = _split(own_terms)
    search = _split(search_terms)
    competitors = _split(competitor_terms)
    categories = _split(category_terms)
    protected = _split(protected_terms)

    competitor_hits = []
    asin_hits = []
    category_hits = []
    protected_hits = []
    expansion = []
    missing_core = []

    for term in search:
        comp = _contains_any(term, competitors)
        cat = _contains_any(term, categories)
        prot = _contains_any(term, protected)
        own_match = _contains_any(term, own)
        if comp:
            competitor_hits.append({"term": term, "matched": comp, "guidance": "竞品流量，先看转化和策略价值，不默认否。"})
        if _asin_like(term):
            asin_hits.append({"term": term, "guidance": "ASIN 串号词，需判断是否竞品页流量或商品关联机会。"})
        if cat and not own_match:
            category_hits.append({"term": term, "matched": cat, "guidance": "类目词覆盖到但自身核心词未命中，检查 Listing/广告结构承接。"})
        if prot:
            protected_hits.append({"term": term, "matched": prot, "guidance": "保护词命中，不应自动否定或激进降 bid。"})
        if not comp and not prot and cat and term not in own:
            expansion.append({"term": term, "reason": "相关类目/属性流量，可作为长尾扩展候选。"})

    for term in own:
        if not _contains_any(term, search):
            missing_core.append({"term": term, "reason": "核心词未在当前搜索词中出现，检查曝光、匹配或预算。"})

    if protected_hits:
        ad_guidance = "先保护品牌/核心词；命中保护词的低效流量优先查承接和出价，不直接否。"
    elif competitor_hits:
        ad_guidance = "竞品流量需要按策略分层：有转化可保留测试，无转化先控 bid/预算而非一刀切否词。"
    elif missing_core:
        ad_guidance = "核心词缺曝光，优先检查广告结构、预算和 Listing 相关性。"
    else:
        ad_guidance = "竞品/类目结构未见明显异常，可结合广告表现继续优化。"

    return {
        "competitor_hits": competitor_hits,
        "asin_hits": asin_hits,
        "category_hits": category_hits,
        "protected_hits": protected_hits,
        "expansion": expansion,
        "missing_core": missing_core,
        "ad_guidance": ad_guidance,
    }


def render(result: dict[str, Any]) -> str:
    lines = ["# 竞品 / 类目关键词诊断", "", f"- 广告动作建议：{result['ad_guidance']}", ""]
    sections = [
        ("竞品流量", "competitor_hits"),
        ("ASIN 串号词", "asin_hits"),
        ("类目覆盖", "category_hits"),
        ("保护词命中", "protected_hits"),
        ("长尾扩展候选", "expansion"),
        ("缺失核心词", "missing_core"),
    ]
    for title, key in sections:
        lines.append(f"## {title}")
        rows = result.get(key) or []
        if not rows:
            lines.append("（无）")
        else:
            for row in rows:
                matched = f" · 命中 {', '.join(row.get('matched', []))}" if row.get("matched") else ""
                reason = row.get("guidance") or row.get("reason") or ""
                lines.append(f"- {row['term']}{matched}: {reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
