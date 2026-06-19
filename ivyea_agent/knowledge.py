"""领域知识（方法论摘要）+ 内置知识包检索。

P1.5 起改为从 GBrain (amazon-ops/*) 检索注入；现在先内置精炼版，保证 LLM
复核与用户方法论对齐。
"""
from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any

ALIASES = {
    "否词": ["negative", "negative targeting", "negative keywords"],
    "否定": ["negative", "negative targeting"],
    "预算": ["budget", "daily budget"],
    "出价": ["bid", "bidding"],
    "竞价": ["bid", "bidding"],
    "搜索词": ["search term", "shopping query"],
    "关键词": ["keyword", "keywords"],
    "匹配": ["match", "match types", "broad", "phrase", "exact"],
    "广泛": ["broad"],
    "词组": ["phrase"],
    "精准": ["exact"],
    "listing": ["listing", "product detail page"],
    "详情页": ["listing", "product detail page"],
    "主图": ["images", "product images"],
    "五点": ["bullet", "bullet points"],
    "转化": ["conversion", "cvr"],
    "acos": ["acos", "roas"],
    "赢家词": ["winner", "winning term", "harvest"],
    "收割": ["harvest", "graduate", "manual exact", "manual phrase"],
    "放量": ["scale", "scaling"],
    "扩量": ["scale", "scaling"],
    "承接": ["listing", "conversion", "product detail page"],
    "新品": ["launch", "new product", "automatic targeting", "discovery"],
    "成熟": ["mature", "mature asin", "efficiency"],
    "报表": ["reports", "search term report", "targeting report", "placement report"],
    "位置": ["placement", "top of search", "product pages"],
    "素材": ["content assets", "content_conversion_assets", "content", "images", "video", "a-plus"],
    "a+": ["a-plus", "A+ Content"],
    "高点击": ["clicks", "high clicks"],
    "零单": ["no orders", "0 orders", "no sales"],
    "无订单": ["no orders", "0 orders"],
}

METHODOLOGY = """\
你是亚马逊广告运营专家，遵循以下方法论（用户长期沉淀）：

目标底线：目标 ACoS ≤ 毛利率；健康 TACoS 10-15%。
优化优先级链（动作排序铁律）：CTR/CVR 杠杆 > 长尾词辅推 > 出价调整 > 否词。

搜索词分类（每词至少一主标签）：品牌词 / 竞品词 / ASIN串号词 / 核心品类词 / 属性词 / 场景词 / 无关词 / 不确定。
动作标签：放量 / 维持测试 / 降bid / 否词候选 / 观察 / Listing反馈 / 人工复核。

否词规则：以 CPA 为首要标尺（CPA>单品利润进考察，二次分析不直接否）；≥15点击0单或高花费0单→控成本/否候选；不建议词根否定（易误伤）；对比近7天 vs 历史30/60天。
不能否：品牌词、竞品词、战略大词、新品期差词、数据不足词、高转化低流量词、疑似 Listing 承接问题而非流量问题的词。

小类目核心词保护：小类目核心大词即使 ACOS 100%+ 也不降 bid/不否（除非语义过宽的无关宽词）。根因是 CTR 低(主图)+CVR 低(Listing/Review)，靠 主图改版/位置溢价(ToS +30~100%, PP归零)/Down only/拆ToS-only守位/副图bullet评论 解决。

异常归因（低效词不要只说"CVR低"）：无关流量 / Listing承接不足 / 价格或转化问题 / 信号混杂 / 需人工看Listing。

护栏铁律：不投 SBV；不走 Vine；拒绝评论操控；不删 campaign；单次降 bid ≤20%；调整后留 5 天稳定期；不否同义词；不预设产品配置（缺信息写"未指定"）。

证据标签：结论绑证据，区分 [报告]（来自搜索词报表数据）与 [推断]（基于现有信息的判断），不把猜测写成事实。
"""


def _base():
    return resources.files("ivyea_agent").joinpath("knowledge_base")


def list_cards() -> list[dict[str, Any]]:
    """Return bundled knowledge card metadata."""
    text = _base().joinpath("index.json").read_text(encoding="utf-8")
    return json.loads(text)


def get_card(card_id: str) -> dict[str, Any] | None:
    for card in list_cards():
        if card["id"] == card_id:
            body = _base().joinpath(card["path"]).read_text(encoding="utf-8")
            return {**card, "body": body}
    return None


def _score(text: str, terms: list[str]) -> int:
    low = text.lower()
    return sum(low.count(t.lower()) for t in terms if t)


def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Simple deterministic search over bundled knowledge cards."""
    terms = re.findall(r"[\w\u4e00-\u9fff+.-]+", query)
    expanded = []
    for t in terms:
        expanded.append(t)
        expanded.extend(ALIASES.get(t.lower(), []))
        expanded.extend(ALIASES.get(t, []))
    for key, vals in ALIASES.items():
        if key in query:
            expanded.append(key)
            expanded.extend(vals)
    terms = expanded
    rows = []
    for card in list_cards():
        body = _base().joinpath(card["path"]).read_text(encoding="utf-8")
        hay = " ".join([card["id"], card["title"], " ".join(card.get("tags", [])), body])
        score = _score(hay, terms)
        if score:
            snippet = _snippet(body, terms)
            rows.append({**card, "score": score, "snippet": snippet})
    rows.sort(key=lambda r: (-r["score"], r["id"]))
    return rows[:limit]


def _snippet(body: str, terms: list[str], width: int = 220) -> str:
    low = body.lower()
    pos = -1
    for t in terms:
        pos = low.find(t.lower())
        if pos >= 0:
            break
    if pos < 0:
        return body[:width].replace("\n", " ").strip()
    start = max(0, pos - width // 3)
    end = min(len(body), start + width)
    return body[start:end].replace("\n", " ").strip()


def render_search(query: str, limit: int = 5) -> str:
    hits = search(query, limit=limit)
    if not hits:
        return "（无匹配知识）"
    lines = []
    for h in hits:
        source = f" · {h['source_url']}" if h.get("source_url") else ""
        lines.append(f"- {h['id']} · {h['title']} [{h['source_type']}]{source}\n  {h['snippet']}")
    return "\n".join(lines)


def context_for_query(query: str, limit: int = 3, max_chars: int = 1200) -> tuple[str, list[str]]:
    """Return compact context snippets for prompt injection plus selected card ids."""
    hits = search(query, limit=limit)
    if not hits:
        return "", []
    lines = []
    ids = []
    for h in hits:
        ids.append(h["id"])
        lines.append(f"[{h['id']}] {h['title']}: {h['snippet']}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n..."
    return text, ids
