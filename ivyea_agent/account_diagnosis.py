"""Account-level Amazon ads diagnosis from search-term style reports.

This is intentionally deterministic and CSV-first. It gives the agent a
broader operating view before it proposes writes: where money is wasted, where
to scale, and which winning terms should be reflected in the listing.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


FIELD_ALIASES = {
    "asin": ("ASIN", "asin", "Advertised ASIN", "advertised_asin"),
    "campaign": ("Campaign Name", "campaign", "campaign_name", "Campaign"),
    "ad_group": ("Ad Group Name", "ad_group", "ad_group_name", "Ad Group"),
    "targeting": ("Targeting", "targeting", "Keyword", "keyword"),
    "match_type": ("Match Type", "match_type", "Match type"),
    "search_term": ("Customer Search Term", "search_term", "Search Term", "Customer Search Term"),
    "impressions": ("Impressions", "impressions", "Impr."),
    "clicks": ("Clicks", "clicks"),
    "spend": ("Spend", "spend", "Cost", "cost"),
    "orders": ("Orders", "orders", "7 Day Total Orders (#)", "Total Orders"),
    "sales": ("Sales", "sales", "7 Day Total Sales", "Total Sales"),
}


def _pick(row: dict[str, Any], key: str, default: str = "") -> str:
    for name in FIELD_ALIASES[key]:
        if name in row and row[name] not in (None, ""):
            return str(row[name]).strip()
    return default


def _num(v: Any) -> float:
    text = str(v or "").strip().replace(",", "").replace("$", "").replace("¥", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text or 0)
    except ValueError:
        return 0.0


def _pct(v: float) -> str:
    return "—" if v <= 0 else f"{v:.1%}"


def _money(v: float) -> str:
    return f"{v:.2f}"


def _metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    impressions = sum(_num(_pick(r, "impressions")) for r in rows)
    clicks = sum(_num(_pick(r, "clicks")) for r in rows)
    spend = sum(_num(_pick(r, "spend")) for r in rows)
    orders = sum(_num(_pick(r, "orders")) for r in rows)
    sales = sum(_num(_pick(r, "sales")) for r in rows)
    return {
        "impressions": impressions,
        "clicks": clicks,
        "spend": spend,
        "orders": orders,
        "sales": sales,
        "ctr": clicks / impressions if impressions else 0.0,
        "cvr": orders / clicks if clicks else 0.0,
        "acos": spend / sales if sales else 0.0,
        "cpc": spend / clicks if clicks else 0.0,
    }


def _group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        buckets[_pick(r, key, "(空)")].append(r)
    out = []
    for name, items in buckets.items():
        m = _metrics(items)
        out.append({"name": name, **m, "rows": len(items)})
    return out


def _tokens(text: str) -> set[str]:
    stop = {"for", "with", "and", "the", "a", "an", "to", "of", "in", "on", "by", "or"}
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if t not in stop}


def load_rows(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到报表: {p}")
    if p.suffix.lower() not in (".csv", ".txt"):
        raise ValueError("account diagnose 当前支持 CSV。xlsx 请先另存为 CSV，或继续用 patrol 规则引擎。")
    with p.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError("报表为空。")
    return rows


def diagnose(csv_path: str, *, target_acos: float = 0.3, listing_text: str = "",
             min_clicks_no_order: int = 12, top_n: int = 8) -> dict[str, Any]:
    rows = load_rows(csv_path)
    total = _metrics(rows)
    by_asin = sorted(_group(rows, "asin"), key=lambda x: x["spend"], reverse=True)
    by_campaign = sorted(_group(rows, "campaign"), key=lambda x: x["spend"], reverse=True)
    by_term = sorted(_group(rows, "search_term"), key=lambda x: x["spend"], reverse=True)

    no_order = [
        t for t in by_term
        if t["orders"] <= 0 and t["clicks"] >= min_clicks_no_order
    ][:top_n]
    high_acos = [
        t for t in by_term
        if t["orders"] > 0 and t["acos"] > target_acos * 1.35
    ][:top_n]
    winners = sorted([
        t for t in by_term
        if t["orders"] >= 2 and t["acos"] > 0 and t["acos"] <= target_acos * 0.85
    ], key=lambda x: (x["orders"], -x["acos"]), reverse=True)[:top_n]
    weak_ctr_campaigns = [
        c for c in by_campaign
        if c["impressions"] >= 500 and c["ctr"] < 0.003
    ][:top_n]
    budget_watch = [
        c for c in by_campaign
        if c["orders"] > 0 and c["acos"] <= target_acos and c["spend"] >= max(10.0, total["spend"] * 0.12)
    ][:top_n]

    listing_tokens = _tokens(listing_text)
    listing_gaps = []
    if listing_tokens and winners:
        for t in winners:
            missing = sorted(_tokens(t["name"]) - listing_tokens)
            if missing:
                listing_gaps.append({"term": t["name"], "missing_tokens": missing[:6], **t})

    priority = []
    if no_order:
        waste = sum(t["spend"] for t in no_order)
        priority.append({
            "level": "P0",
            "title": f"先处理 {len(no_order)} 个高点击零单词，已浪费 {_money(waste)}",
            "action": "否词或降探索预算；品牌/核心词先人工复核。",
        })
    if high_acos:
        priority.append({
            "level": "P1",
            "title": f"压住 {len(high_acos)} 个高 ACOS 有单词",
            "action": "小步降 bid，优先保留有订单的精准流量。",
        })
    if winners:
        priority.append({
            "level": "P1",
            "title": f"放大 {len(winners)} 个健康转化词",
            "action": "收割进精准/词组活动，检查预算是否限制。",
        })
    if listing_gaps:
        priority.append({
            "level": "P2",
            "title": f"把 {len(listing_gaps)} 个赢家词补进 Listing 语义",
            "action": "优先补副图、五点、A+，不要硬塞标题。",
        })

    return {
        "source": str(Path(csv_path).resolve()),
        "target_acos": target_acos,
        "total": total,
        "by_asin": by_asin[:top_n],
        "by_campaign": by_campaign[:top_n],
        "no_order": no_order,
        "high_acos": high_acos,
        "winners": winners,
        "weak_ctr_campaigns": weak_ctr_campaigns,
        "budget_watch": budget_watch,
        "listing_gaps": listing_gaps[:top_n],
        "priority": priority,
    }


def _table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> list[str]:
    if not rows:
        return ["（无）"]
    out = ["| " + " | ".join(label for label, _ in cols) + " |",
           "| " + " | ".join("---" for _ in cols) + " |"]
    for r in rows:
        vals = []
        for _, key in cols:
            v = r.get(key, "")
            if key in ("spend", "sales", "cpc"):
                vals.append(_money(float(v or 0)))
            elif key in ("ctr", "cvr", "acos"):
                vals.append(_pct(float(v or 0)))
            elif key in ("clicks", "orders", "impressions"):
                vals.append(str(int(float(v or 0))))
            elif key == "missing_tokens":
                vals.append(", ".join(v or []))
            else:
                vals.append(str(v))
        out.append("| " + " | ".join(vals) + " |")
    return out


def render_md(result: dict[str, Any]) -> str:
    t = result["total"]
    lines = [
        "# 亚马逊广告账户诊断",
        "",
        f"- 数据源：`{result['source']}`",
        f"- 目标 ACOS：{result['target_acos']:.0%}",
        f"- 总览：曝光 {int(t['impressions'])} / 点击 {int(t['clicks'])} / 花费 {_money(t['spend'])} / "
        f"订单 {int(t['orders'])} / 销售 {_money(t['sales'])} / ACOS {_pct(t['acos'])} / CVR {_pct(t['cvr'])}",
        "",
        "## 优先级行动",
    ]
    if result["priority"]:
        for p in result["priority"]:
            lines.append(f"- **{p['level']} {p['title']}**：{p['action']}")
    else:
        lines.append("- 暂无强信号；建议继续积累数据，重点看预算撞顶和 Listing 承接。")

    sections = [
        ("## 高点击零单浪费", result["no_order"],
         [("搜索词", "name"), ("点击", "clicks"), ("花费", "spend"), ("CPC", "cpc")]),
        ("## 高 ACOS 有单词", result["high_acos"],
         [("搜索词", "name"), ("点击", "clicks"), ("订单", "orders"), ("花费", "spend"), ("销售", "sales"), ("ACOS", "acos")]),
        ("## 健康赢家词", result["winners"],
         [("搜索词", "name"), ("订单", "orders"), ("花费", "spend"), ("销售", "sales"), ("ACOS", "acos"), ("CVR", "cvr")]),
        ("## 活动预算观察", result["budget_watch"],
         [("活动", "name"), ("花费", "spend"), ("订单", "orders"), ("ACOS", "acos"), ("CVR", "cvr")]),
        ("## 低 CTR 活动", result["weak_ctr_campaigns"],
         [("活动", "name"), ("曝光", "impressions"), ("点击", "clicks"), ("CTR", "ctr"), ("花费", "spend")]),
        ("## ASIN 花费分布", result["by_asin"],
         [("ASIN", "name"), ("花费", "spend"), ("订单", "orders"), ("销售", "sales"), ("ACOS", "acos")]),
        ("## Listing 语义缺口", result["listing_gaps"],
         [("赢家词", "term"), ("缺少词根", "missing_tokens"), ("订单", "orders"), ("ACOS", "acos")]),
    ]
    for title, rows, cols in sections:
        lines.extend(["", title, "", *_table(rows, cols)])
    return "\n".join(lines)
