"""Persistent local retrieval index.

这一层**刻意固定用词频稀疏向量**，不跟随 `retrieval_embeddings` 的后端配置。

原因是规模：索引里是上千个分块（本机实测 1538 个），用内置的真模型编码一遍要 6 分钟，
而 `search()` 在索引缺失或过期时会**同步重建**——那就是一次搜索卡死几分钟。
稀疏向量零成本、确定性、随时可重建，正是这一层要的性质。

语义检索发生在别处：分类记忆的检索（`memory_store.search` → `memory_vectors`）走内置
模型的稠密向量，并且有时间护栏。让知识卡也用上稠密向量是后续工作，前提是先解决
"上千分块怎么增量向量化而不卡住任何一次搜索"。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import hashlib
from pathlib import Path
from typing import Any

from . import config, knowledge, memory, retrieval_embeddings


BACKEND = "local_hash_embedding_v1"


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
    indexed_fingerprint = ""
    emb_status = retrieval_embeddings.status()
    # 这一层固定稀疏（见模块说明），所以 backend 报的就是 BACKEND，
    # 不跟随 embedding 配置——否则换个后端就会把索引标记成"需要重建"，
    # 而重建出来的其实还是同一批稀疏向量。
    vector_backend = BACKEND
    vector_kind = emb_status["vector_kind"]
    if exists:
        conn = _conn()
        chunks = int(conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"])
        cards = int(conn.execute("SELECT COUNT(DISTINCT source_id) c FROM chunks WHERE source='knowledge'").fetchone()["c"])
        memory_chunks = int(conn.execute("SELECT COUNT(*) c FROM chunks WHERE source='memory'").fetchone()["c"])
        row = conn.execute("SELECT value FROM meta WHERE key='updated_at'").fetchone()
        updated_at = row["value"] if row else ""
        row = conn.execute("SELECT value FROM meta WHERE key='vector_backend'").fetchone()
        vector_backend = row["value"] if row else vector_backend
        row = conn.execute("SELECT value FROM meta WHERE key='vector_kind'").fetchone()
        vector_kind = row["value"] if row else vector_kind
        row = conn.execute("SELECT value FROM meta WHERE key='source_fingerprint'").fetchone()
        indexed_fingerprint = row["value"] if row else ""
        conn.close()
    else:
        memory_chunks = 0
    current_fingerprint = source_fingerprint(emb_status=emb_status)["fingerprint"]
    needs_rebuild = (not exists) or chunks <= 0 or indexed_fingerprint != current_fingerprint
    return {
        "enabled": exists and chunks > 0,
        "backend": vector_backend,
        "index_backend": BACKEND,
        "vector_kind": vector_kind,
        "external_dependency": bool(emb_status.get("external_dependency")),
        "db": str(path),
        "chunks": chunks,
        "knowledge_cards": cards,
        "memory_chunks": memory_chunks,
        "sources": {"knowledge": cards, "memory": memory_chunks},
        "updated_at": updated_at,
        "source_fingerprint": current_fingerprint,
        "indexed_fingerprint": indexed_fingerprint,
        "needs_rebuild": needs_rebuild,
        "embeddings": emb_status,
    }


def source_fingerprint(*, emb_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """给 sync 用的廉价变更检测：只看知识与记忆的内容。

    `emb_status` 参数保留是为了不改调用方签名——但**刻意不再参与指纹**：这一层固定
    稀疏向量，换 embedding 后端不影响它存的东西，算进去只会导致无谓的重建。
    """
    del emb_status
    knowledge_parts = []
    for card in knowledge.list_cards():
        knowledge_parts.append("|".join([
            str(card.get("id", "")),
            str(card.get("body_hash", "")),
            str(card.get("freshness", "")),
            str(card.get("source_quality", "")),
        ]))
    memory_parts = []
    for row in memory.index_rows():
        memory_parts.append("|".join([
            str(row.get("rowid") or ""),
            str(row.get("ts") or ""),
            _hash(str(row.get("text") or "")),
        ]))
    payload = {
        # 指纹里刻意**不含** embedding 后端：这一层固定稀疏，换后端不影响它存的向量。
        # 含进去的话，用户一改 embedding 配置就会被判定"索引过期"，重建出来还是同一批东西。
        "backend": BACKEND,
        "vector_kind": "sparse",
        "knowledge": sorted(knowledge_parts),
        "memory": sorted(memory_parts),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return {
        "fingerprint": _hash(raw),
        "knowledge_cards": len(knowledge_parts),
        "memory_rows": len(memory_parts),
        "backend": BACKEND,
        "vector_kind": "sparse",
    }


def sync() -> dict[str, Any]:
    """Rebuild the index only when source or embedding fingerprints changed."""
    st = status()
    if not st.get("needs_rebuild"):
        return {
            "ok": True,
            "changed": False,
            "backend": st.get("backend", ""),
            "index_backend": st.get("index_backend", BACKEND),
            "vector_kind": st.get("vector_kind", ""),
            "chunks": st.get("chunks", 0),
            "knowledge_cards": st.get("knowledge_cards", 0),
            "memory_chunks": st.get("memory_chunks", 0),
            "sources": st.get("sources") or {},
            "db": st.get("db", str(db_path())),
            "updated_at": st.get("updated_at", ""),
            "source_fingerprint": st.get("source_fingerprint", ""),
            "indexed_fingerprint": st.get("indexed_fingerprint", ""),
            "embeddings": st.get("embeddings") or retrieval_embeddings.status(),
        }
    rebuilt = rebuild()
    rebuilt["changed"] = True
    return rebuilt


def rebuild() -> dict[str, Any]:
    conn = _conn()
    conn.execute("DELETE FROM chunks WHERE source IN ('knowledge', 'memory')")
    now = time.time()
    emb = retrieval_embeddings.status()
    fp = source_fingerprint(emb_status=emb)
    chunk_count = 0
    card_count = 0
    memory_count = 0
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
            vector = retrieval_embeddings.encode_sparse(vector_text)
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
    for row in memory.index_rows():
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        rowid = str(row.get("rowid") or "")
        ts = float(row.get("ts") or now)
        asin = str(row.get("asin") or "")
        source_id = f"memory:{rowid or int(ts)}"
        tags = ["memory"] + ([asin] if asin else [])
        vector_text = " ".join([asin, text])
        vector = retrieval_embeddings.encode_sparse(vector_text)
        conn.execute(
            "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id, "memory", source_id, asin or "memory", 1, text,
                "user", "memory", "user_supplied", "local",
                "account_local_memory", "", json.dumps(tags, ensure_ascii=False),
                _hash(text), json.dumps(vector, ensure_ascii=False), ts,
            ),
        )
        chunk_count += 1
        memory_count += 1
    _set_meta(conn, "backend", BACKEND)
    _set_meta(conn, "vector_backend", BACKEND)
    _set_meta(conn, "vector_kind", "sparse")
    _set_meta(conn, "updated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))
    _set_meta(conn, "source_fingerprint", fp["fingerprint"])
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "changed": True,
        "backend": BACKEND,
        "index_backend": BACKEND,
        "vector_kind": "sparse",
        "knowledge_cards": card_count,
        "memory_chunks": memory_count,
        "sources": {"knowledge": card_count, "memory": memory_count},
        "chunks": chunk_count,
        "db": str(db_path()),
        "source_fingerprint": fp["fingerprint"],
        "indexed_fingerprint": fp["fingerprint"],
        "embeddings": emb,
    }


def search(query: str, limit: int = 8, sources: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    if not db_path().exists() or status()["chunks"] == 0:
        rebuild()
    qvec = retrieval_embeddings.encode_sparse_query(q)
    if not qvec:
        return []
    vector_backend = str(qvec.get("backend") or BACKEND)
    wanted = _normal_sources(sources)
    conn = _conn()
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(f"SELECT * FROM chunks WHERE source IN ({placeholders})", wanted).fetchall()
    conn.close()
    hits = []
    terms = _query_terms(q)
    for row in rows:
        vec = retrieval_embeddings.decode(row["vector_json"])
        sim = retrieval_embeddings.cosine(qvec, vec)
        if sim <= 0:
            continue
        text = row["text"] or ""
        source = str(row["source"] or "")
        hits.append({
            "source": "memory" if source == "memory" else "knowledge_index",
            "id": row["id"],
            "source_id": row["source_id"],
            "title": row["title"],
            "snippet": _snippet(text, terms),
            "score": int(12 + sim * 80),
            # 报**实际使用的向量后端**，不是索引实现的名字（index_backend 才是那个）。
            # 硬编码 BACKEND 的话，dense 后端下这条会谎称命中来自 hash 稀疏向量。
            "match": vector_backend,
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


def _normal_sources(sources: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    allowed = {"knowledge", "memory"}
    wanted = tuple(s for s in (sources or ("knowledge", "memory")) if s in allowed)
    return wanted or ("knowledge", "memory")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


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
