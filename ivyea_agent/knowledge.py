"""领域知识（方法论摘要）+ 内置知识包检索。

P1.5 起改为从 GBrain (amazon-ops/*) 检索注入；现在先内置精炼版，保证 LLM
复核与用户方法论对齐。
"""
from __future__ import annotations

import json
import re
import hashlib
import sqlite3
import difflib
import html
import io
import zipfile
from importlib import resources
from pathlib import Path
import time
from datetime import datetime
from typing import Any

from . import config, security

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
    "来源": ["source", "source quality", "confidence", "freshness"],
    "置信": ["confidence", "source quality"],
    "时效": ["freshness", "retrieved_at", "version"],
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

SOURCE_WATCHLIST = [
    {
        "id": "amazon_ads.sponsored_products",
        "title": "Amazon Ads Sponsored Products",
        "source_type": "official",
        "url": "https://advertising.amazon.com/solutions/products/sponsored-products",
        "category": "amazon_ads",
        "tags": ["sponsored-products", "campaign", "targeting", "budget", "bidding"],
        "license": "amazon_public_docs_summary",
        "priority": 100,
        "review_note": "广告产品、投放入口、展示位置、预算和报告能力的官方基准来源。",
    },
    {
        "id": "amazon_ads.api_docs",
        "title": "Amazon Ads API Documentation",
        "source_type": "official",
        "url": "https://advertising.amazon.com/API/docs/en-us/get-started/overview",
        "category": "amazon_ads_api",
        "tags": ["ads-api", "reporting", "campaign-management", "automation"],
        "license": "amazon_public_docs_summary",
        "priority": 95,
        "review_note": "广告 API 能力、认证、报表和自动化动作的官方入口。",
    },
    {
        "id": "amazon_sp_api.docs",
        "title": "Amazon Selling Partner API Documentation",
        "source_type": "official",
        "url": "https://developer-docs.amazon.com/sp-api/",
        "category": "sp_api",
        "tags": ["sp-api", "catalog", "inventory", "orders", "reports"],
        "license": "amazon_public_docs_summary",
        "priority": 95,
        "review_note": "Seller/Vendor API、模型、Release Notes 和授权流程的官方入口。",
    },
    {
        "id": "amazon_seller.a_plus_content",
        "title": "Amazon A+ Content",
        "source_type": "official",
        "url": "https://sell.amazon.com/tools/a-content",
        "category": "listing",
        "tags": ["a-plus", "listing", "conversion", "content"],
        "license": "amazon_public_docs_summary",
        "priority": 85,
        "review_note": "Listing 承接、A+ 内容和转化素材判断的官方来源。",
    },
    {
        "id": "amazon_seller.fba",
        "title": "Fulfillment by Amazon",
        "source_type": "official",
        "url": "https://sell.amazon.com/fulfillment-by-amazon",
        "category": "inventory",
        "tags": ["fba", "inventory", "fulfillment", "stockout"],
        "license": "amazon_public_docs_summary",
        "priority": 75,
        "review_note": "库存、履约、断货风险与广告放量联动判断的官方来源。",
    },
    {
        "id": "amazon_ads.blog",
        "title": "Amazon Ads Blog / Guides",
        "source_type": "official_plus_blog",
        "url": "https://advertising.amazon.com/library/guides",
        "category": "amazon_ads",
        "tags": ["guide", "launch", "optimization", "case-study"],
        "license": "amazon_public_docs_summary",
        "priority": 70,
        "review_note": "官方指南和案例可辅助方法论，但导入时要与产品帮助页交叉验证。",
    },
    {
        "id": "amazon_seller_forums",
        "title": "Amazon Seller Forums",
        "source_type": "community_official_forum",
        "url": "https://sellercentral.amazon.com/seller-forums",
        "category": "community",
        "tags": ["seller-forum", "policy", "operations", "case"],
        "license": "community_summary_requires_review",
        "priority": 55,
        "review_note": "适合发现真实运营问题和边界案例；不能覆盖官方规则，必须人工复核。",
    },
    {
        "id": "zhiwubuyan",
        "title": "知无不言跨境电商社区",
        "source_type": "community",
        "url": "https://www.wearesellers.com/",
        "category": "community",
        "tags": ["community", "seller-case", "china-seller", "operations"],
        "license": "community_summary_requires_review",
        "priority": 45,
        "review_note": "适合沉淀中文卖家经验和案例；导入前必须去广告化、去个人隐私、去未经验证结论。",
    },
]

IMPORTABLE_SUFFIXES = {
    ".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".html", ".htm", ".docx", ".xlsx", ".xlsm", ".pdf",
}
IGNORED_IMPORT_DIRS = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv"}
DEFAULT_IMPORT_FILE_BYTES = 5 * 1024 * 1024


def _base():
    return resources.files("ivyea_agent").joinpath("knowledge_base")


def _user_base() -> Path:
    return config.IVYEA_DIR / "knowledge"


def _sources_file() -> Path:
    return _user_base() / "sources.jsonl"


def _index_file() -> Path:
    return _user_base() / "index.db"


def _uploads_dir() -> Path:
    return _user_base() / "uploads"


def _upload_history_file() -> Path:
    return _user_base() / "uploads.jsonl"


def list_cards() -> list[dict[str, Any]]:
    """Return bundled and user knowledge card metadata."""
    text = _base().joinpath("index.json").read_text(encoding="utf-8")
    cards = json.loads(text)
    for card in cards:
        _enrich_builtin_card(card)
    cards.extend(list_user_cards())
    return cards


def list_builtin_cards() -> list[dict[str, Any]]:
    text = _base().joinpath("index.json").read_text(encoding="utf-8")
    rows = json.loads(text)
    for card in rows:
        _enrich_builtin_card(card)
    return rows


def _enrich_builtin_card(card: dict[str, Any]) -> dict[str, Any]:
    card.setdefault("retrieved_at", card.get("version", ""))
    card.setdefault("confidence", _confidence(card.get("source_type", "")))
    card.setdefault("freshness", _freshness(card))
    card.setdefault("source_quality", _source_quality(card))
    card.setdefault("license", "amazon_public_docs_summary")
    card.setdefault("scope", "builtin")
    try:
        body = _base().joinpath(card["path"]).read_text(encoding="utf-8")
        card.setdefault("body_hash", _hash(body))
    except (KeyError, OSError, UnicodeDecodeError):
        card.setdefault("body_hash", "")
    return card


def list_user_cards() -> list[dict[str, Any]]:
    p = _sources_file()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            card = json.loads(line)
        except Exception:
            continue
        card.setdefault("source_type", "user")
        card.setdefault("confidence", _confidence(card.get("source_type", "")))
        card.setdefault("retrieved_at", "")
        card.setdefault("freshness", _freshness(card))
        card.setdefault("source_quality", _source_quality(card))
        card.setdefault("license", "user_supplied")
        if not card.get("body_hash"):
            try:
                card["body_hash"] = _hash(_user_base().joinpath(card["path"]).read_text(encoding="utf-8"))
            except Exception:
                card["body_hash"] = ""
        card.setdefault("scope", "user")
        rows.append(card)
    return rows


def _confidence(source_type: str) -> str:
    if source_type == "official":
        return "high"
    if source_type.startswith("official_plus"):
        return "medium_high"
    if source_type.startswith("community"):
        return "medium"
    if source_type == "user":
        return "user_supplied"
    return "unknown"


def _source_quality(card: dict[str, Any]) -> str:
    source_type = str(card.get("source_type") or "")
    scope = str(card.get("scope") or "")
    if source_type == "official":
        return "authoritative"
    if source_type.startswith("official_plus"):
        return "synthesized_with_official_anchor"
    if source_type.startswith("community"):
        return "directional_requires_account_validation"
    if scope == "user" or source_type == "user":
        return "account_local_overrides_generic_knowledge"
    return "unknown_requires_review"


def _freshness(card: dict[str, Any]) -> str:
    stamp = str(card.get("retrieved_at") or card.get("version") or "").strip()
    if not stamp:
        return "undated"
    parsed = None
    for fmt in ("%Y-%m-%d", "%Y.%m", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(stamp, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return "reviewed"
    now = datetime.fromtimestamp(time.time())
    months = (now.year - parsed.year) * 12 + (now.month - parsed.month)
    if months <= 6:
        return "current"
    if months <= 12:
        return "aging_review_soon"
    return "stale_needs_review"


def get_card(card_id: str) -> dict[str, Any] | None:
    for card in list_cards():
        if card["id"] == card_id:
            body = _read_body(card)
            return {**card, "body": body}
    return None


def _read_body(card: dict[str, Any]) -> str:
    if card.get("scope") == "user":
        p = _user_base().joinpath(card["path"])
        return p.read_text(encoding="utf-8")
    return _base().joinpath(card["path"]).read_text(encoding="utf-8")


def _score(text: str, terms: list[str]) -> int:
    low = text.lower()
    return sum(low.count(t.lower()) for t in terms if t)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Simple deterministic search over bundled and user knowledge cards."""
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
        body = _read_body(card)
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
        meta = (
            f"{h['source_type']} confidence={h.get('confidence', 'unknown')} "
            f"freshness={h.get('freshness', '-')} quality={h.get('source_quality', '-')}"
        )
        lines.append(f"- {h['id']} · {h['title']} [{meta}]{source}\n  {h['snippet']}")
    return "\n".join(lines)


def render_audit() -> str:
    """Show source quality metadata for bundled and user knowledge cards."""
    lines = ["Ivyea 知识库审计："]
    for card in audit()["cards"]:
        source = card.get("source_url") or "-"
        lines.append(
            f"- {card['id']} | {card.get('scope', 'builtin')} | {card['source_type']} | confidence={card.get('confidence')} | "
            f"freshness={card.get('freshness')} | quality={card.get('source_quality')} | "
            f"retrieved={card.get('retrieved_at')} | license={card.get('license', '-')} | hash={str(card.get('body_hash', ''))[:12]} | source={source}"
        )
    return "\n".join(lines)


def source_registry() -> dict[str, Any]:
    """Return a product-facing source registry grouped by URL/type/license."""
    cards = list_cards()
    grouped: dict[str, dict[str, Any]] = {}
    for card in cards:
        source_url = str(card.get("source_url") or "").strip()
        source_type = str(card.get("source_type") or "unknown")
        license_name = str(card.get("license") or "unknown")
        category = str(card.get("category") or "uncategorized")
        scope = str(card.get("scope") or "builtin")
        key = source_url or f"{scope}:{source_type}:{category}:{license_name}"
        row = grouped.setdefault(key, {
            "key": key,
            "source_url": source_url,
            "source_type": source_type,
            "scope": scope,
            "license": license_name,
            "categories": set(),
            "cards": [],
            "card_count": 0,
            "stale_cards": 0,
            "missing_source_url": not bool(source_url),
            "confidence_levels": set(),
            "freshness_levels": set(),
            "source_qualities": set(),
        })
        row["categories"].add(category)
        row["confidence_levels"].add(str(card.get("confidence") or "unknown"))
        row["freshness_levels"].add(str(card.get("freshness") or "unknown"))
        row["source_qualities"].add(str(card.get("source_quality") or "unknown"))
        row["cards"].append({
            "id": card.get("id", ""),
            "title": card.get("title", ""),
            "category": category,
            "freshness": card.get("freshness", ""),
            "confidence": card.get("confidence", ""),
            "body_hash": card.get("body_hash", ""),
        })
        row["card_count"] += 1
        if card.get("freshness") == "stale_needs_review":
            row["stale_cards"] += 1

    sources = []
    for row in grouped.values():
        item = dict(row)
        item["categories"] = sorted(row["categories"])
        item["confidence_levels"] = sorted(row["confidence_levels"])
        item["freshness_levels"] = sorted(row["freshness_levels"])
        item["source_qualities"] = sorted(row["source_qualities"])
        item["review_required"] = bool(item["stale_cards"]) or (
            bool(item["missing_source_url"]) and item["source_type"].startswith("community")
        )
        sources.append(item)
    sources.sort(key=lambda r: (r["scope"] != "builtin", r["source_type"], r["key"]))
    summary = {
        "cards": len(cards),
        "sources": len(sources),
        "official_sources": len([s for s in sources if s["source_type"] == "official"]),
        "community_sources": len([s for s in sources if str(s["source_type"]).startswith("community")]),
        "user_sources": len([s for s in sources if s["scope"] == "user" or s["source_type"] == "user"]),
        "missing_source_url_cards": len([c for c in cards if not c.get("source_url")]),
        "stale_sources": len([s for s in sources if s["stale_cards"]]),
        "review_required_sources": len([s for s in sources if s["review_required"]]),
        "licenses": _counts([str(c.get("license") or "unknown") for c in cards]),
        "categories": _counts([str(c.get("category") or "uncategorized") for c in cards]),
    }
    return {"summary": summary, "sources": sources}


def render_source_registry() -> str:
    data = source_registry()
    s = data["summary"]
    lines = [
        "Ivyea 知识来源登记表：",
        f"- cards={s['cards']} sources={s['sources']} official={s['official_sources']} "
        f"community={s['community_sources']} user={s['user_sources']} review_required={s['review_required_sources']}",
    ]
    for source in data["sources"]:
        url = source.get("source_url") or "(no source_url)"
        flags = []
        if source.get("missing_source_url"):
            flags.append("missing-url")
        if source.get("review_required"):
            flags.append("review")
        flag_text = f" [{' '.join(flags)}]" if flags else ""
        categories = ",".join(source.get("categories") or [])
        card_ids = ",".join(c.get("id", "") for c in source.get("cards") or [])
        lines.append(
            f"- {source['source_type']} | {source['scope']} | {source['license']} | "
            f"cards={source['card_count']} | categories={categories}{flag_text}\n"
            f"  {url}\n"
            f"  ids={card_ids}"
        )
    return "\n".join(lines)


def source_watchlist() -> dict[str, Any]:
    """Return curated sources that IvyeaAgent should monitor/import from with review."""
    rows = []
    for source in SOURCE_WATCHLIST:
        row = dict(source)
        source_type = str(row.get("source_type") or "")
        row["review_required"] = (
            source_type.startswith("community")
            or row.get("license") == "community_summary_requires_review"
        )
        row["confidence"] = _confidence(source_type)
        rows.append(row)
    rows.sort(key=lambda r: (-int(r.get("priority") or 0), r.get("id", "")))
    summary = {
        "sources": len(rows),
        "official_sources": len([r for r in rows if r["source_type"] == "official"]),
        "community_sources": len([r for r in rows if str(r["source_type"]).startswith("community")]),
        "review_required_sources": len([r for r in rows if r.get("review_required")]),
        "categories": _counts([str(r.get("category") or "uncategorized") for r in rows]),
        "policy": "manual_review_before_import",
    }
    return {"summary": summary, "sources": rows}


def render_source_watchlist() -> str:
    data = source_watchlist()
    s = data["summary"]
    lines = [
        "Ivyea Amazon 知识来源观察清单：",
        f"- sources={s['sources']} official={s['official_sources']} "
        f"community={s['community_sources']} review_required={s['review_required_sources']} "
        f"policy={s['policy']}",
    ]
    for source in data["sources"]:
        review = " review" if source.get("review_required") else ""
        tags = ",".join(source.get("tags") or [])
        lines.append(
            f"- {source['id']} | {source['source_type']} | priority={source.get('priority', 0)}{review}\n"
            f"  {source['title']}\n"
            f"  {source['url']}\n"
            f"  tags={tags}\n"
            f"  note={source.get('review_note', '')}"
        )
    return "\n".join(lines)


def _counts(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda item: item[0]))


def audit() -> dict[str, Any]:
    """Return structured source quality metadata for product integrations."""
    cards = []
    for card in list_cards():
        cards.append({
            "id": card.get("id", ""),
            "title": card.get("title", ""),
            "category": card.get("category", ""),
            "scope": card.get("scope", "builtin"),
            "source_type": card.get("source_type", ""),
            "confidence": card.get("confidence", ""),
            "freshness": card.get("freshness", ""),
            "source_quality": card.get("source_quality", ""),
            "retrieved_at": card.get("retrieved_at", ""),
            "license": card.get("license", ""),
            "source_url": card.get("source_url", ""),
            "tags": list(card.get("tags") or []),
            "body_hash": card.get("body_hash", ""),
        })
    conflict_rows = conflicts()
    registry = source_registry()
    summary = {
        "cards": len(cards),
        "builtin_cards": len([c for c in cards if c.get("scope") != "user"]),
        "user_cards": len([c for c in cards if c.get("scope") == "user"]),
        "official_cards": len([c for c in cards if str(c.get("source_type") or "") == "official"]),
        "stale_cards": len([c for c in cards if c.get("freshness") == "stale_needs_review"]),
        "conflicts": len(conflict_rows),
        "source_registry": registry["summary"],
        "sources": str(_sources_file()),
        "index": str(_index_file()),
    }
    return {"summary": summary, "cards": cards, "conflicts": conflict_rows, "source_registry": registry}


def context_for_query(query: str, limit: int = 3, max_chars: int = 1200) -> tuple[str, list[str]]:
    """Return compact context snippets for prompt injection plus selected card ids."""
    hits = search(query, limit=limit)
    if not hits:
        return "", []
    lines = []
    ids = []
    for h in hits:
        ids.append(h["id"])
        lines.append(
            f"[{h['id']}] {h['title']} "
            f"(confidence={h.get('confidence', 'unknown')}, freshness={h.get('freshness', '-')}, "
            f"quality={h.get('source_quality', '-')})：{h['snippet']}"
        )
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n..."
    return text, ids


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug[:80] or time.strftime("knowledge-%Y%m%d-%H%M%S")


def _safe_filename(filename: str) -> str:
    name = Path(str(filename or "upload")).name
    stem = _slug(Path(name).stem or "upload")
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", Path(name).suffix.lower())[:16]
    return f"{stem}{suffix}" if suffix else stem


def _safe_path_segment(segment: str, fallback: str = "item") -> str:
    raw = str(segment or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._").lower()
    if cleaned:
        return cleaned[:80]
    return f"{fallback}-{_hash(raw or fallback)[:12]}"


def _first_heading_or_stem(body: str, fallback: str) -> str:
    for line in str(body or "").splitlines()[:20]:
        text = line.strip()
        if text.startswith("#"):
            title = text.lstrip("#").strip()
            if title:
                return title[:160]
    return str(fallback or "导入知识").strip()[:160] or "导入知识"


def _safe_rel_path(path: str) -> Path:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("../") or "/../" in raw or raw == "..":
        raise ValueError("invalid knowledge file path")
    rel = Path(raw)
    if rel.parts and rel.parts[0] not in {"user", "uploads"}:
        raise ValueError("knowledge file path must be under user/ or uploads/")
    return rel


def _resolve_user_path(path: str) -> Path:
    rel = _safe_rel_path(path)
    base = _user_base().resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise ValueError("knowledge file path escapes knowledge directory")
    return target


def _tag_list(tags: list[str] | str | None) -> list[str]:
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    return []


def _upload_rows() -> list[dict[str, Any]]:
    p = _upload_history_file()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _upsert_upload(row: dict[str, Any]) -> None:
    p = _upload_history_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in _upload_rows() if r.get("id") != row.get("id")]
    rows.append(row)
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    tmp.replace(p)


def list_uploads(limit: int = 50) -> dict[str, Any]:
    rows = _upload_rows()[:max(1, min(int(limit or 50), 200))]
    return {"root": str(_uploads_dir()), "uploads": rows}


def upload_detail(upload_id: str) -> dict[str, Any] | None:
    for row in _upload_rows():
        if row.get("id") == upload_id:
            return row
    return None


def list_files(limit: int = 500) -> dict[str, Any]:
    """Return user knowledge cards and uploaded source files for product UIs."""
    root = _user_base()
    uploads = []
    if _uploads_dir().exists():
        for p in sorted(_uploads_dir().rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            uploads.append({
                "path": rel,
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": int(p.stat().st_mtime),
                "kind": "extracted" if p.name == "extracted.md" else "raw",
            })
            if len(uploads) >= limit:
                break
    cards = []
    for card in list_user_cards():
        cards.append({
            "id": card.get("id", ""),
            "title": card.get("title", ""),
            "path": card.get("path", ""),
            "tags": list(card.get("tags") or []),
            "source_type": card.get("source_type", ""),
            "source_url": card.get("source_url", ""),
            "license": card.get("license", ""),
            "body_hash": card.get("body_hash", ""),
            "retrieved_at": card.get("retrieved_at", ""),
        })
    return {
        "root": str(root),
        "uploads_root": str(_uploads_dir()),
        "uploads": uploads,
        "cards": cards,
        "history": _upload_rows()[:max(1, min(int(limit or 500), 200))],
    }


def read_file(path: str, max_chars: int = 200_000) -> dict[str, Any]:
    target = _resolve_user_path(path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(path)
    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {
        "path": _safe_rel_path(path).as_posix(),
        "name": target.name,
        "size": target.stat().st_size,
        "mtime": int(target.stat().st_mtime),
        "content": security.redact_text(text),
        "truncated": truncated,
    }


def delete_file(path: str) -> dict[str, Any]:
    rel = _safe_rel_path(path)
    target = _resolve_user_path(rel.as_posix())
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(path)
    target.unlink()
    removed_card_ids = []
    if rel.parts and rel.parts[0] == "user":
        rows = []
        for card in list_user_cards():
            if card.get("path") == rel.as_posix():
                removed_card_ids.append(str(card.get("id") or ""))
            else:
                rows.append(card)
        p = _sources_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
        rebuild_index()
    return {"ok": True, "path": rel.as_posix(), "removed_card_ids": removed_card_ids}


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _html_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|header|footer|nav)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _extract_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml).strip()


def _extract_xlsx(data: bytes) -> str:
    try:
        from openpyxl import load_workbook
    except Exception:
        return ""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return ""
    lines = []
    for ws in wb.worksheets[:8]:
        lines.append(f"## {ws.title}")
        for row in ws.iter_rows(max_row=500, values_only=True):
            vals = [str(v).strip() if v is not None else "" for v in row]
            if any(vals):
                lines.append(" | ".join(vals))
    return "\n".join(lines).strip()


def _extract_pdf(data: bytes) -> str:
    reader_cls = None
    try:
        from pypdf import PdfReader
        reader_cls = PdfReader
    except Exception:
        try:
            from PyPDF2 import PdfReader
            reader_cls = PdfReader
        except Exception:
            return ""
    try:
        reader = reader_cls(io.BytesIO(data))
        pages = []
        for page in reader.pages[:80]:
            pages.append(page.extract_text() or "")
        return "\n\n".join(pages).strip()
    except Exception:
        return ""


def extract_document_text(filename: str, data: bytes) -> dict[str, Any]:
    suffix = Path(filename or "").suffix.lower()
    warnings: list[str] = []
    if suffix in {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log"}:
        text = _decode_text(data)
    elif suffix in {".html", ".htm"}:
        text = _html_to_text(_decode_text(data))
    elif suffix == ".docx":
        text = _extract_docx(data)
        if not text:
            warnings.append("docx_text_extraction_failed")
    elif suffix in {".xlsx", ".xlsm"}:
        text = _extract_xlsx(data)
        if not text:
            warnings.append("xlsx_text_extraction_unavailable")
    elif suffix == ".pdf":
        text = _extract_pdf(data)
        if not text:
            warnings.append("pdf_text_extraction_unavailable")
    else:
        text = _decode_text(data)
        if len(text.strip()) < 20:
            warnings.append("unknown_binary_or_empty_text")
    text = security.redact_text(text).strip()
    return {"text": text, "warnings": warnings, "extension": suffix or ""}


def upload_document(filename: str, data: bytes, *, title: str = "", source_url: str = "",
                    source_type: str = "user", confidence: str = "",
                    tags: list[str] | str | None = None, card_id: str = "",
                    license: str = "user_supplied", confirm: bool = False,
                    rebuild_indexes: bool = True) -> dict[str, Any]:
    if not data:
        raise ValueError("empty upload")
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("upload too large; max 25MB")
    config.ensure_dirs()
    safe_name = _safe_filename(filename)
    upload_id = f"up-{time.strftime('%Y%m%d-%H%M%S')}-{_hash(safe_name + str(len(data)) + str(time.time()))[:8]}"
    day = time.strftime("%Y%m%d")
    raw_rel = Path("uploads") / day / upload_id / safe_name
    raw_path = _user_base() / raw_rel
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(data)

    extracted = extract_document_text(safe_name, data)
    text = str(extracted.get("text") or "").strip()
    warnings = list(extracted.get("warnings") or [])
    if not text:
        warnings.append("no_text_extracted")
        text = f"# {title or Path(safe_name).stem}\n\n（未能自动抽取文本，请在 IvyeaOps 中补充可检索正文。）"

    clean_title = str(title or Path(safe_name).stem or "上传知识").strip()
    extracted_rel = Path("uploads") / day / upload_id / "extracted.md"
    extracted_path = _user_base() / extracted_rel
    extracted_path.parent.mkdir(parents=True, exist_ok=True)
    extracted_body = text if text.startswith("#") else f"# {clean_title}\n\n{text}"
    extracted_path.write_text(extracted_body.strip() + "\n", encoding="utf-8")

    effective_source_url = source_url or f"ivyea-upload://{upload_id}/{safe_name}"
    draft = draft_update(
        clean_title,
        extracted_body,
        source_url=effective_source_url,
        source_type=source_type or "user",
        confidence=confidence,
        tags=tags,
        card_id=card_id,
        license=license,
    )
    row = {
        "id": upload_id,
        "filename": safe_name,
        "title": clean_title,
        "raw_path": raw_rel.as_posix(),
        "extracted_path": extracted_rel.as_posix(),
        "size": len(data),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_url": effective_source_url,
        "source_type": source_type or "user",
        "confidence": confidence or _confidence(source_type or "user"),
        "license": license or "user_supplied",
        "tags": _tag_list(tags),
        "card_id": draft.get("card_id", ""),
        "warnings": warnings,
        "text_chars": len(extracted_body),
        "body_hash": _hash(extracted_body),
    }
    _upsert_upload(row)
    result: dict[str, Any] = {
        "ok": True,
        "upload": row,
        "extraction": {
            "text_chars": len(extracted_body),
            "warnings": warnings,
            "preview": extracted_body[:1200],
        },
        "draft": draft,
    }
    if confirm:
        result["apply"] = apply_update(draft, confirm=True, rebuild_indexes=rebuild_indexes)
    return result


def apply_upload(upload_id: str, *, confirm: bool = False, rebuild_indexes: bool = True) -> dict[str, Any]:
    row = upload_detail(upload_id)
    if not row:
        raise FileNotFoundError(upload_id)
    extracted_path = _resolve_user_path(str(row.get("extracted_path") or ""))
    body = extracted_path.read_text(encoding="utf-8", errors="replace")
    draft = draft_update(
        str(row.get("title") or row.get("filename") or upload_id),
        body,
        source_url=str(row.get("source_url") or ""),
        source_type=str(row.get("source_type") or "user"),
        confidence=str(row.get("confidence") or ""),
        tags=row.get("tags") if isinstance(row.get("tags"), list) else [],
        card_id=str(row.get("card_id") or ""),
        license=str(row.get("license") or "user_supplied"),
    )
    result = apply_update(draft, confirm=confirm, rebuild_indexes=rebuild_indexes)
    if result.get("ok") and result.get("applied"):
        row["import_status"] = "imported"
        row["imported_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _upsert_upload(row)
    return {"ok": bool(result.get("ok")), "upload": row, "draft": draft, "result": result}


def _limited_diff(old_body: str, new_body: str, *, old_label: str, new_label: str, limit: int = 12000) -> str:
    lines = list(difflib.unified_diff(
        old_body.splitlines(),
        new_body.splitlines(),
        fromfile=old_label,
        tofile=new_label,
        lineterm="",
    ))
    text = "\n".join(lines)
    if len(text) > limit:
        return text[:limit].rstrip() + "\n... diff truncated ..."
    return text


def _safe_update_card_id(title: str, card_id: str = "") -> tuple[str, list[str]]:
    warnings = []
    raw = str(card_id or "").strip()
    if not raw:
        return f"user.{_slug(title)}", warnings
    if raw.startswith("user."):
        return raw, warnings
    warnings.append("card_id_not_user_scoped: imported as user.<id> to avoid overriding bundled knowledge")
    return f"user.{_slug(raw)}", warnings


def draft_update(title: str, body: str, *, source_url: str = "", source_type: str = "user",
                 confidence: str = "", tags: list[str] | str | None = None, card_id: str = "",
                 license: str = "user_supplied") -> dict[str, Any]:
    """Prepare a reviewed knowledge update without mutating local files."""
    clean_title = str(title or "").strip() or "用户知识"
    clean_body = security.redact_text(str(body or "")).strip()
    if not clean_body:
        raise ValueError("body is required")
    clean_body += "\n"
    clean_source_type = str(source_type or "user").strip() or "user"
    tag_list = _tag_list(tags)
    safe_id, warnings = _safe_update_card_id(clean_title, card_id)

    if not source_url:
        warnings.append("missing_source_url")
    if clean_source_type == "official" and not source_url:
        warnings.append("official_source_without_url")
    if clean_source_type.startswith("community"):
        warnings.append("community_source_requires_manual_verification")
    if len(clean_body.strip()) < 80:
        warnings.append("short_body_review_recommended")

    existing = get_card(safe_id)
    old_body = ""
    old_hash = ""
    if existing:
        old_body = security.redact_text(str(existing.get("body") or "")).strip() + "\n"
        old_hash = existing.get("body_hash") or _hash(old_body)

    new_hash = _hash(clean_body)
    if existing and old_hash == new_hash:
        action = "noop"
    elif existing:
        action = "update"
    else:
        action = "create"

    diff = "" if action == "noop" else _limited_diff(
        old_body,
        clean_body,
        old_label=f"{safe_id}:old",
        new_label=f"{safe_id}:new",
    )
    review_required = (
        clean_source_type.startswith("community")
        or not source_url
        or "short_body_review_recommended" in warnings
    )
    return {
        "ok": True,
        "action": action,
        "card_id": safe_id,
        "title": clean_title,
        "source_url": str(source_url or "").strip(),
        "source_type": clean_source_type,
        "confidence": str(confidence or _confidence(clean_source_type)),
        "license": str(license or "user_supplied"),
        "tags": tag_list,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "old_scope": existing.get("scope") if existing else "",
        "diff": diff,
        "body": clean_body,
        "warnings": warnings,
        "review_required": review_required,
    }


def draft_update_from_file(path: str, *, title: str = "", source_url: str = "",
                           source_type: str = "user", confidence: str = "",
                           tags: list[str] | str | None = None, card_id: str = "",
                           license: str = "user_supplied") -> dict[str, Any]:
    p = Path(path).expanduser().resolve()
    body = p.read_text(encoding="utf-8", errors="replace")
    return draft_update(
        title or p.stem,
        body,
        source_url=source_url or str(p),
        source_type=source_type,
        confidence=confidence,
        tags=tags,
        card_id=card_id,
        license=license,
    )


def apply_update(draft: dict[str, Any], *, confirm: bool = False,
                 rebuild_indexes: bool = True) -> dict[str, Any]:
    """Apply a previously reviewed draft. Confirmation is mandatory."""
    if not isinstance(draft, dict) or not draft.get("ok"):
        return {"ok": False, "applied": False, "error": "invalid_draft"}
    if not confirm:
        return {
            "ok": False,
            "applied": False,
            "error": "confirmation_required",
            "draft": _draft_summary(draft),
        }
    if draft.get("action") == "noop":
        return {"ok": True, "applied": False, "action": "noop", "draft": _draft_summary(draft)}

    body = str(draft.get("body") or "").strip()
    if not body:
        return {"ok": False, "applied": False, "error": "draft_body_missing"}
    card = import_text(
        str(draft.get("title") or draft.get("card_id") or "用户知识"),
        body,
        source_url=str(draft.get("source_url") or ""),
        source_type=str(draft.get("source_type") or "user"),
        confidence=str(draft.get("confidence") or ""),
        tags=_tag_list(draft.get("tags")),
        card_id=str(draft.get("card_id") or ""),
        license=str(draft.get("license") or "user_supplied"),
    )
    indexes: dict[str, Any] = {}
    if rebuild_indexes:
        indexes["knowledge"] = rebuild_index()
        try:
            from . import retrieval
            indexes["retrieval"] = retrieval.rebuild_index()
        except Exception as exc:
            indexes["retrieval_error"] = security.redact_text(str(exc))
    return {
        "ok": True,
        "applied": True,
        "action": draft.get("action"),
        "card": card,
        "indexes": indexes,
        "warnings": list(draft.get("warnings") or []),
        "conflicts": conflicts(),
    }


def _draft_summary(draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(draft.get("ok")),
        "action": draft.get("action", ""),
        "card_id": draft.get("card_id", ""),
        "title": draft.get("title", ""),
        "source_type": draft.get("source_type", ""),
        "source_url": draft.get("source_url", ""),
        "old_hash": draft.get("old_hash", ""),
        "new_hash": draft.get("new_hash", ""),
        "warnings": list(draft.get("warnings") or []),
        "review_required": bool(draft.get("review_required")),
    }


def render_update_draft(draft: dict[str, Any]) -> str:
    if not draft.get("ok"):
        return f"知识更新草案生成失败：{draft.get('error', 'unknown')}"
    lines = [
        "Ivyea 知识更新草案：",
        f"- action={draft.get('action')} id={draft.get('card_id')} title={draft.get('title')}",
        f"- source={draft.get('source_type')} {draft.get('source_url') or '(missing source_url)'}",
        f"- old_hash={str(draft.get('old_hash') or '-')[:12]} new_hash={str(draft.get('new_hash') or '-')[:12]}",
        f"- review_required={bool(draft.get('review_required'))}",
    ]
    if draft.get("warnings"):
        lines.append("- warnings=" + ",".join(str(w) for w in draft.get("warnings") or []))
    diff = str(draft.get("diff") or "")
    if diff:
        lines.extend(["", diff])
    else:
        lines.append("")
        lines.append("无内容变更。")
    return "\n".join(lines)


def render_update_apply(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"知识更新未应用：{result.get('error', 'unknown')}"
    if not result.get("applied"):
        return f"知识更新无需应用：action={result.get('action', 'noop')}"
    card = result.get("card") or {}
    indexes = result.get("indexes") or {}
    knowledge_index = indexes.get("knowledge") or {}
    retrieval_index = indexes.get("retrieval") or {}
    return (
        f"已应用知识更新：{card.get('id')} -> {card.get('path')}\n"
        f"knowledge_index cards={knowledge_index.get('cards', '-')}\n"
        f"retrieval_index chunks={retrieval_index.get('chunks', '-')}"
    )


def import_text(title: str, body: str, *, source_url: str = "", source_type: str = "user",
                confidence: str = "", tags: list[str] | None = None, card_id: str = "",
                license: str = "user_supplied") -> dict[str, Any]:
    """Import a user knowledge card into ~/.ivyea/knowledge."""
    config.ensure_dirs()
    base = _user_base()
    base.mkdir(parents=True, exist_ok=True)
    safe_id = card_id or f"user.{_slug(title)}"
    rel = f"user/{_slug(safe_id)}.md"
    out = base / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    clean_body = security.redact_text(body).strip() + "\n"
    out.write_text(clean_body, encoding="utf-8")
    card = {
        "id": safe_id,
        "title": title.strip() or safe_id,
        "category": "user",
        "source_type": source_type or "user",
        "confidence": confidence or _confidence(source_type or "user"),
        "retrieved_at": time.strftime("%Y-%m-%d"),
        "license": license or "user_supplied",
        "source_url": source_url,
        "path": rel,
        "tags": tags or [],
        "scope": "user",
        "body_hash": _hash(clean_body),
    }
    _upsert_source(card)
    return card


def _import_destination(namespace: str, rel: Path) -> tuple[str, str]:
    safe_namespace = _safe_path_segment(namespace or "imported", "imported")
    parts = [_safe_path_segment(part, "dir") for part in rel.with_suffix("").parts]
    if not parts:
        parts = ["item"]
    file_stem = parts[-1]
    dir_parts = parts[:-1]
    body_key = rel.as_posix()
    card_id = f"user.{safe_namespace}.{_hash(body_key)[:16]}"
    out_rel = Path("user") / "imported" / safe_namespace / Path(*dir_parts) / f"{file_stem}.md"
    return card_id, out_rel.as_posix()


def _scan_import_file(root: Path, path: Path, *, namespace: str, max_file_bytes: int) -> dict[str, Any]:
    rel = path.relative_to(root)
    rel_text = rel.as_posix()
    size = path.stat().st_size
    base = {
        "source_path": rel_text,
        "size": size,
        "extension": path.suffix.lower(),
    }
    if any(part.startswith(".") or part in IGNORED_IMPORT_DIRS for part in rel.parts):
        return {**base, "importable": False, "reason": "hidden_or_ignored_path"}
    if path.suffix.lower() not in IMPORTABLE_SUFFIXES:
        return {**base, "importable": False, "reason": "unsupported_extension"}
    if size > max_file_bytes:
        return {**base, "importable": False, "reason": "file_too_large"}
    data = path.read_bytes()
    extracted = extract_document_text(path.name, data)
    body = str(extracted.get("text") or "").strip()
    if not body:
        return {**base, "importable": False, "reason": "no_text_extracted", "warnings": extracted.get("warnings") or []}
    if not body.startswith("#"):
        body = f"# {_first_heading_or_stem(body, path.stem)}\n\n{body}"
    body = body.strip() + "\n"
    card_id, target_path = _import_destination(namespace, rel)
    existing = get_card(card_id)
    body_hash = _hash(body)
    old_hash = str(existing.get("body_hash") or "") if existing else ""
    if existing and old_hash == body_hash:
        action = "noop"
    elif existing:
        action = "update"
    else:
        action = "create"
    return {
        **base,
        "importable": True,
        "action": action,
        "card_id": card_id,
        "title": _first_heading_or_stem(body, path.stem),
        "target_path": target_path,
        "source_url": f"{namespace}://{rel_text}",
        "tags": [namespace, *[str(p) for p in rel.parts[:-1]]],
        "warnings": extracted.get("warnings") or [],
        "text_chars": len(body),
        "body_hash": body_hash,
        "old_hash": old_hash,
        "body": body,
    }


def import_directory(root: str, *, namespace: str = "gbrain", confirm: bool = False,
                     max_files: int = 1000, max_file_bytes: int = DEFAULT_IMPORT_FILE_BYTES,
                     rebuild_indexes: bool = True) -> dict[str, Any]:
    """Scan or import a legacy markdown knowledge directory into user knowledge.

    This is intentionally file-based and does not depend on the old GBrain CLI.
    It lets IvyeaOps inherit ~/brain-style knowledge into ~/.ivyea/knowledge.
    """
    root_path = Path(root or "").expanduser().resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise FileNotFoundError(str(root_path))
    safe_namespace = _safe_path_segment(namespace or "gbrain", "gbrain")
    limit = max(1, min(int(max_files or 1000), 5000))
    byte_limit = max(1024, min(int(max_file_bytes or DEFAULT_IMPORT_FILE_BYTES), 25 * 1024 * 1024))
    scanned = 0
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root_path)
        except ValueError:
            continue
        if any(part.startswith(".") or part in IGNORED_IMPORT_DIRS for part in rel.parts):
            continue
        scanned += 1
        row = _scan_import_file(root_path, path, namespace=safe_namespace, max_file_bytes=byte_limit)
        if row.get("importable"):
            public_row = {k: v for k, v in row.items() if k != "body"}
            candidates.append(public_row)
        else:
            skipped.append(row)
        if len(candidates) >= limit:
            break

    imported: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    if confirm:
        base = _user_base()
        for row in candidates:
            if row.get("action") == "noop":
                unchanged.append(row)
                continue
            source_file = root_path / str(row["source_path"])
            body = _scan_import_file(root_path, source_file, namespace=safe_namespace, max_file_bytes=byte_limit).get("body", "")
            if not body:
                continue
            target = (base / str(row["target_path"])).resolve()
            if base.resolve() not in target.parents:
                raise ValueError("import target escapes knowledge directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            clean_body = security.redact_text(str(body)).strip() + "\n"
            target.write_text(clean_body, encoding="utf-8")
            card = {
                "id": row["card_id"],
                "title": row.get("title") or row["card_id"],
                "category": f"legacy_{safe_namespace}",
                "source_type": f"legacy_{safe_namespace}",
                "confidence": "user_supplied",
                "retrieved_at": time.strftime("%Y-%m-%d"),
                "license": "user_supplied",
                "source_url": row.get("source_url") or "",
                "path": row["target_path"],
                "tags": row.get("tags") or [safe_namespace],
                "scope": "user",
                "body_hash": _hash(clean_body),
            }
            _upsert_source(card)
            imported.append({**row, "card": card})

    indexes: dict[str, Any] = {}
    if confirm and rebuild_indexes:
        indexes["knowledge"] = rebuild_index()
    return {
        "ok": True,
        "root": str(root_path),
        "namespace": safe_namespace,
        "confirm": bool(confirm),
        "scanned_files": scanned,
        "candidates": candidates,
        "skipped": skipped[:200],
        "summary": {
            "candidate_files": len(candidates),
            "skipped_files": len(skipped),
            "create": len([r for r in candidates if r.get("action") == "create"]),
            "update": len([r for r in candidates if r.get("action") == "update"]),
            "noop": len([r for r in candidates if r.get("action") == "noop"]),
            "imported": len(imported),
            "unchanged": len(unchanged),
            "limit_reached": len(candidates) >= limit,
        },
        "imported": imported,
        "unchanged": unchanged,
        "indexes": indexes,
    }


def import_file(path: str, *, title: str = "", source_type: str = "user",
                confidence: str = "", tags: list[str] | None = None, card_id: str = "",
                license: str = "user_supplied") -> dict[str, Any]:
    p = Path(path).expanduser().resolve()
    body = p.read_text(encoding="utf-8", errors="replace")
    return import_text(
        title or p.stem,
        body,
        source_url=str(p),
        source_type=source_type,
        confidence=confidence,
        tags=tags,
        card_id=card_id,
        license=license,
    )


def import_url(url: str, *, title: str = "", source_type: str = "user",
               confidence: str = "", tags: list[str] | None = None, card_id: str = "",
               license: str = "user_supplied") -> dict[str, Any]:
    import httpx

    r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "ivyea-agent/0.4"})
    r.raise_for_status()
    text = r.text
    if "html" in r.headers.get("content-type", ""):
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
    return import_text(
        title or url,
        text,
        source_url=url,
        source_type=source_type,
        confidence=confidence,
        tags=tags,
        card_id=card_id,
        license=license,
    )


def _upsert_source(card: dict[str, Any]) -> None:
    p = _sources_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [c for c in list_user_cards() if c.get("id") != card["id"]]
    rows.append(card)
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    tmp.replace(p)


def rebuild() -> dict[str, Any]:
    """Validate user knowledge metadata and prune rows with missing files."""
    rows = []
    missing = []
    for card in list_user_cards():
        if _user_base().joinpath(card["path"]).exists():
            rows.append(card)
        else:
            missing.append(card["id"])
    p = _sources_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
    idx = rebuild_index()
    return {"user_cards": len(rows), "missing_pruned": missing, "sources": str(p), "index": idx}


def _conn() -> sqlite3.Connection:
    p = _index_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS cards (
        id TEXT PRIMARY KEY,
        title TEXT,
        category TEXT,
        scope TEXT,
        source_type TEXT,
        confidence TEXT,
        freshness TEXT,
        source_quality TEXT,
        retrieved_at TEXT,
        license TEXT,
        body_hash TEXT,
        tags TEXT,
        source_url TEXT,
        body TEXT
    )""")
    _ensure_column(conn, "cards", "category", "TEXT")
    _ensure_column(conn, "cards", "freshness", "TEXT")
    _ensure_column(conn, "cards", "source_quality", "TEXT")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _fts_ok(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(id, title, tags, body)")
        return True
    except Exception:
        return False


def rebuild_index() -> dict[str, Any]:
    conn = _conn()
    fts = _fts_ok(conn)
    conn.execute("DELETE FROM cards")
    if fts:
        conn.execute("DELETE FROM cards_fts")
    count = 0
    for card in list_cards():
        body = _read_body(card)
        body_hash = card.get("body_hash") or _hash(body)
        tags = ",".join(card.get("tags") or [])
        conn.execute(
            "INSERT OR REPLACE INTO cards "
            "(id,title,category,scope,source_type,confidence,freshness,source_quality,retrieved_at,license,body_hash,tags,source_url,body) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                card["id"], card.get("title", ""), card.get("category", ""),
                card.get("scope", "builtin"),
                card.get("source_type", ""), card.get("confidence", ""),
                card.get("freshness", ""), card.get("source_quality", ""),
                card.get("retrieved_at", ""), card.get("license", ""),
                body_hash, tags, card.get("source_url", ""), body,
            ),
        )
        if fts:
            conn.execute("INSERT INTO cards_fts (id, title, tags, body) VALUES (?,?,?,?)",
                         (card["id"], card.get("title", ""), tags, body))
        count += 1
    conn.commit()
    conn.close()
    return {"cards": count, "fts": fts, "db": str(_index_file()), "source_registry": source_registry()["summary"]}


def search_index(query: str, limit: int = 5) -> list[dict[str, Any]]:
    if not _index_file().exists():
        rebuild_index()
    conn = _conn()
    rows = []
    try:
        if _fts_ok(conn):
            rows = conn.execute(
                "SELECT c.* FROM cards_fts f JOIN cards c ON c.id=f.id "
                "WHERE cards_fts MATCH ? LIMIT ?",
                (query, limit),
            ).fetchall()
        if not rows:
            like = f"%{query}%"
            rows = conn.execute(
                "SELECT * FROM cards WHERE id LIKE ? OR title LIKE ? OR tags LIKE ? OR body LIKE ? LIMIT ?",
                (like, like, like, like, limit),
            ).fetchall()
    except Exception:
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT * FROM cards WHERE id LIKE ? OR title LIKE ? OR tags LIKE ? OR body LIKE ? LIMIT ?",
            (like, like, like, like, limit),
        ).fetchall()
    conn.close()
    out = []
    for row in rows:
        d = dict(row)
        d["snippet"] = _snippet(d.get("body", ""), re.findall(r"[\w\u4e00-\u9fff+.-]+", query))
        d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
        out.append(d)
    return out


def conflicts() -> list[dict[str, Any]]:
    cards = list_cards()
    official = [c for c in cards if c.get("source_type") == "official" or str(c.get("source_type", "")).startswith("official_plus")]
    user = [c for c in cards if c.get("scope") == "user"]
    rows = []
    for card in user:
        if not card.get("license"):
            rows.append({"level": "warn", "id": card["id"], "reason": "用户知识卡缺 license"})
        tags = set(card.get("tags") or [])
        body = _read_body(card).lower()
        reverse = any(k in body for k in ("不要", "禁止", "不建议", "avoid", "do not", "never"))
        if not reverse:
            continue
        overlaps = [o["id"] for o in official if tags and tags.intersection(set(o.get("tags") or []))]
        if overlaps:
            rows.append({
                "level": "review",
                "id": card["id"],
                "reason": "用户/社区知识含反向表述，且标签与官方知识重叠；需要人工确认是否冲突",
                "related": overlaps[:5],
            })
    return rows


def render_conflicts() -> str:
    rows = conflicts()
    if not rows:
        return "Ivyea 知识库冲突审计\n\nOK 未发现明显冲突风险。\n"
    lines = ["Ivyea 知识库冲突审计", ""]
    for r in rows:
        related = f" related={','.join(r.get('related', []))}" if r.get("related") else ""
        lines.append(f"- [{r['level']}] {r['id']}: {r['reason']}{related}")
    return "\n".join(lines) + "\n"
