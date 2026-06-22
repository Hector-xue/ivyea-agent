"""Persistent local retrieval index.

This is a no-dependency retrieval substrate for IvyeaOps integration. It stores
knowledge chunks plus a deterministic sparse vector in SQLite. The backend is
not a neural embedding model; it is a stable local fallback that can be replaced
by a downloaded embedding model later without changing the service contract.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import config, knowledge


BACKEND = "local_hash_embedding_v1"
QUERY_ALIASES = {
    "主图": "main image hero image",
    "图片": "image creative content",
    "转化": "conversion convert cvr",
    "否词": "negative keyword negative targeting",
    "预算": "budget",
    "竞价": "bid bidding",
    "出价": "bid bidding",
    "点击": "click ctr",
    "订单": "order orders",
    "广告": "advertising sponsored products",
    "搜索词": "search term query",
    "关键词": "keyword targeting",
    "竞品": "competitor conquesting",
    "评论": "review reviews",
    "库存": "inventory stock",
    "利润": "profit margin",
    "价格": "price offer",
    "放量": "scaling scale budget bid",
}


def db_path() -> Path:
    return config.IVYEA_DIR / "retrieval" / "index.db"


def _conn() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY,
        source TEXT,
        source_id TEXT,
        title TEXT,
        chunk_index INTEGER,
        text TEXT,
        scope TEXT,
        source_type TEXT,
        confidence TEXT,
        freshness TEXT,
        source_quality TEXT,
        source_url TEXT,
        tags TEXT,
        body_hash TEXT,
        vector_json TEXT,
        updated_at REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    return conn


def status() -> dict[str, Any]:
    path = db_path()
    exists = path.exists()
    chunks = 0
    cards = 0
    updated_at = ""
    if exists:
        conn = _conn()
        chunks = int(conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"])
        cards = int(conn.execute("SELECT COUNT(DISTINCT source_id) c FROM chunks WHERE source='knowledge'").fetchone()["c"])
        row = conn.execute("SELECT value FROM meta WHERE key='updated_at'").fetchone()
        updated_at = row["value"] if row else ""
        conn.close()
    return {
        "enabled": exists and chunks > 0,
        "backend": BACKEND,
        "external_dependency": False,
        "db": str(path),
        "chunks": chunks,
        "knowledge_cards": cards,
        "updated_at": updated_at,
    }


def rebuild() -> dict[str, Any]:
    conn = _conn()
    conn.execute("DELETE FROM chunks WHERE source='knowledge'")
    now = time.time()
    chunk_count = 0
    card_count = 0
    for card in knowledge.list_cards():
        full = knowledge.get_card(card["id"]) or card
        body = str(full.get("body") or "")
        if not body.strip():
            continue
        card_count += 1
        for i, text in enumerate(_chunk_text(body), start=1):
            chunk_id = f"knowledge:{card['id']}:{i}"
            vector_text = " ".join([
                str(card.get("id", "")),
                str(card.get("title", "")),
                " ".join(card.get("tags") or []),
                text,
            ])
            vector = _vector(vector_text)
            conn.execute(
                "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    chunk_id, "knowledge", card["id"], card.get("title", ""), i, text,
                    card.get("scope", "builtin"), card.get("source_type", ""),
                    card.get("confidence", ""), card.get("freshness", ""),
                    card.get("source_quality", ""), card.get("source_url", ""),
                    json.dumps(card.get("tags") or [], ensure_ascii=False),
                    card.get("body_hash", ""), json.dumps(vector, ensure_ascii=False), now,
                ),
            )
            chunk_count += 1
    _set_meta(conn, "backend", BACKEND)
    _set_meta(conn, "updated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))
    conn.commit()
    conn.close()
    return {"ok": True, "backend": BACKEND, "knowledge_cards": card_count, "chunks": chunk_count, "db": str(db_path())}


def search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    if not db_path().exists() or status()["chunks"] == 0:
        rebuild()
    qvec = _vector(_expand_query(q))
    if not qvec:
        return []
    conn = _conn()
    rows = conn.execute("SELECT * FROM chunks").fetchall()
    conn.close()
    hits = []
    terms = _query_terms(q)
    for row in rows:
        vec = _load_vector(row["vector_json"])
        sim = _cosine(qvec, vec)
        if sim <= 0:
            continue
        text = row["text"] or ""
        hits.append({
            "source": "knowledge_index",
            "id": row["id"],
            "source_id": row["source_id"],
            "title": row["title"],
            "snippet": _snippet(text, terms),
            "score": int(12 + sim * 80),
            "match": BACKEND,
            "vector_score": round(sim, 4),
            "scope": row["scope"],
            "source_type": row["source_type"],
            "confidence": row["confidence"],
            "freshness": row["freshness"],
            "source_quality": row["source_quality"],
            "source_url": row["source_url"],
            "tags": _json_list(row["tags"]),
            "body_hash": row["body_hash"],
        })
    hits.sort(key=lambda h: (-float(h.get("score") or 0), h.get("id", "")))
    return hits[:max(1, min(int(limit or 8), 50))]


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _chunk_text(text: str, size: int = 1200, overlap: int = 160) -> list[str]:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []
    chunks = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + size)
        chunks.append(clean[start:end])
        if end >= len(clean):
            break
        start = max(0, end - overlap)
    return chunks


def _query_terms(text: str) -> list[str]:
    return re.findall(r"[\w\u4e00-\u9fff+.-]+", text)


def _expand_query(text: str) -> str:
    extras = [alias for term, alias in QUERY_ALIASES.items() if term.lower() in text.lower()]
    return " ".join([text, *extras])


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9+.-]+|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            if len(raw) <= 2:
                tokens.append(raw)
            else:
                tokens.extend(raw[i:i + 2] for i in range(len(raw) - 1))
        else:
            tokens.append(raw)
    return tokens


def _vector(text: str) -> dict[str, float]:
    counts = Counter(_tokens(text))
    total = sum(counts.values()) or 1
    return {k: round(v / total, 8) for k, v in counts.items()}


def _load_vector(raw: str) -> dict[str, float]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0.0) for key, value in left.items())
    if dot <= 0:
        return 0.0
    ln = math.sqrt(sum(v * v for v in left.values()))
    rn = math.sqrt(sum(v * v for v in right.values()))
    return dot / (ln * rn) if ln and rn else 0.0


def _snippet(body: str, terms: list[str], width: int = 240) -> str:
    low = body.lower()
    pos = -1
    for term in terms:
        pos = low.find(term.lower())
        if pos >= 0:
            break
    if pos < 0:
        return body[:width].strip()
    start = max(0, pos - width // 3)
    return body[start:start + width].strip()


def _json_list(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []
