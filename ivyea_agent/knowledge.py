"""领域知识（方法论摘要）+ 内置知识包检索。

P1.5 起改为从 GBrain (amazon-ops/*) 检索注入；现在先内置精炼版，保证 LLM
复核与用户方法论对齐。
"""
from __future__ import annotations

import json
import re
import hashlib
import sqlite3
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


def _base():
    return resources.files("ivyea_agent").joinpath("knowledge_base")


def _user_base() -> Path:
    return config.IVYEA_DIR / "knowledge"


def _sources_file() -> Path:
    return _user_base() / "sources.jsonl"


def _index_file() -> Path:
    return _user_base() / "index.db"


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
    for card in list_cards():
        source = card.get("source_url") or "-"
        lines.append(
            f"- {card['id']} | {card.get('scope', 'builtin')} | {card['source_type']} | confidence={card.get('confidence')} | "
            f"freshness={card.get('freshness')} | quality={card.get('source_quality')} | "
            f"retrieved={card.get('retrieved_at')} | license={card.get('license', '-')} | hash={str(card.get('body_hash', ''))[:12]} | source={source}"
        )
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
        scope TEXT,
        source_type TEXT,
        confidence TEXT,
        retrieved_at TEXT,
        license TEXT,
        body_hash TEXT,
        tags TEXT,
        source_url TEXT,
        body TEXT
    )""")
    return conn


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
            "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                card["id"], card.get("title", ""), card.get("scope", "builtin"),
                card.get("source_type", ""), card.get("confidence", ""),
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
    return {"cards": count, "fts": fts, "db": str(_index_file())}


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
