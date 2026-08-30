"""Persistent local retrieval index.

默认仍是词频稀疏向量，但**稠密向量现在可以打开**（设置 `retrieval_index_dense`）。

原来挡在稠密前面的是两件事，都已经解决：

1. `rebuild()` 是整张表删掉重来，改一个字也要把上千个分块全部重编——稀疏下只是浪费，
   稠密下就是每次改动卡住几分钟。现在走 `sync_incremental()`，按 `vector_sig`
   （内容 + 后端 + 档位）只重编真正变了的分块，没变的原样留着。
2. `search()` 会在索引缺失时**同步重建**，等于把重建塞进热路径。现在搜索**绝不重建**，
   索引不在就返回空，让调用方退回词法；重建只由显式命令和定时任务触发。

默认仍然关着稠密，是因为**打开后的第一次同步要把全部分块过一遍真模型**（本机 2292 块
约 2.5 分钟）。那种一次性开销不该藏在用户随便一条命令里发生。

查询侧按每个分块**实际存的向量种类**编码：`cosine` 在 kind 不一致时直接返 0，
存了稠密却拿稀疏查询去比，会让整个索引静默失效。
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
    # 在线迁移：老库没有这三列。增量同步靠 vector_sig 判断"这块要不要重编"，
    # vector_kind/backend 让稀疏与稠密可以共存于同一张表（换后端时只重编受影响的）。
    have = {row["name"] for row in conn.execute("PRAGMA table_info(chunks)")}
    for column in ("vector_kind", "vector_backend", "vector_sig"):
        if column not in have:
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {column} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
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


#: 知识分块是否用稠密向量。**默认关**：打开后第一次同步要把上千个分块过一遍真模型
#: （本机实测 1538 块约 6 分钟）。那种一次性开销绝不能藏在用户随便一条命令里发生，
#: 必须由 `ivyea retrieval rebuild` 这类显式动作触发。
DENSE_SETTING = "retrieval_index_dense"


def dense_enabled() -> bool:
    try:
        if not bool(config.get_setting(DENSE_SETTING, False)):
            return False
    except Exception:      # noqa: BLE001
        return False
    return bool(retrieval_embeddings.status().get("semantic_enabled"))


def _encode_chunk(text: str) -> dict[str, Any]:
    """按当前档位编码一个分块。稠密没开就走零成本的稀疏。"""
    if dense_enabled():
        return retrieval_embeddings.encode_document(text)
    return retrieval_embeddings.encode_sparse(text)


def _vector_signature(vector_text: str) -> str:
    """决定"这一块要不要重新编码"的签名。

    带上档位与后端：换了 embedding 后端或开关了稠密，签名变、才重编；只是别的卡
    改了内容，这一块签名没变就**原样留着**——这正是增量的全部意义。
    """
    st = retrieval_embeddings.status() if dense_enabled() else {}
    marker = f"dense:{st.get('active_backend', '')}:{st.get('model', '')}" if st else "sparse"
    return _hash(f"{marker}\n{vector_text}")


def _desired_chunks() -> list[tuple[str, tuple, str]]:
    """算出索引**应该**是什么样：[(chunk_id, 行数据, 用于编码的文本)]。"""
    now = time.time()
    out: list[tuple[str, tuple, str]] = []
    for card in knowledge.list_cards():
        full = knowledge.get_card(card["id"]) or card
        body = str(full.get("body") or "")
        if not body.strip():
            continue
        for i, text in enumerate(_chunk_text(body), start=1):
            chunk_id = f"knowledge:{card['id']}:{i}"
            vector_text = " ".join([
                str(card.get("id", "")), str(card.get("title", "")),
                " ".join(card.get("tags") or []), text,
            ])
            out.append((chunk_id, (
                chunk_id, "knowledge", card["id"], card.get("title", ""), i, text,
                card.get("scope", "builtin"), card.get("source_type", ""),
                card.get("confidence", ""), card.get("freshness", ""),
                card.get("source_quality", ""), card.get("source_url", ""),
                json.dumps(card.get("tags") or [], ensure_ascii=False),
                card.get("body_hash", ""), now,
            ), vector_text))
    for row in memory.index_rows():
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        rowid = str(row.get("rowid") or "")
        ts = float(row.get("ts") or now)
        asin = str(row.get("asin") or "")
        source_id = f"memory:{rowid or int(ts)}"
        tags = ["memory"] + ([asin] if asin else [])
        out.append((source_id, (
            source_id, "memory", source_id, asin or "memory", 1, text,
            "user", "memory", "user_supplied", "local", "account_local_memory", "",
            json.dumps(tags, ensure_ascii=False), _hash(text), ts,
        ), " ".join([asin, text])))
    return out


def sync_incremental(progress: Any = None) -> dict[str, Any]:
    """只重编真正变了的分块。

    原来的 `rebuild()` 是"整张表删掉重来"，于是任何一张卡改一个字都要把上千个分块
    全部重新编码一遍。稀疏向量下这只是浪费；一旦要上稠密向量，那就是每次改动都
    卡住六分钟——**这正是稠密向量一直上不了的真正原因**，不是模型不行。
    """
    conn = _conn()
    stored = {
        row["id"]: (row["vector_sig"] or "")
        for row in conn.execute("SELECT id, vector_sig FROM chunks")
    }
    desired = _desired_chunks()
    desired_ids = {chunk_id for chunk_id, _row, _text in desired}

    removed = [cid for cid in stored if cid not in desired_ids]
    for cid in removed:
        conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))

    encoded = 0
    reused = 0
    total = len(desired)
    for index, (chunk_id, row, vector_text) in enumerate(desired, 1):
        signature = _vector_signature(vector_text)
        if stored.get(chunk_id) == signature:
            reused += 1
            continue
        vector = _encode_chunk(vector_text)
        conn.execute(
            "INSERT OR REPLACE INTO chunks (id, source, source_id, title, chunk_index, text,"
            " scope, source_type, confidence, freshness, source_quality, source_url, tags,"
            " body_hash, updated_at, vector_json, vector_kind, vector_backend, vector_sig)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row + (
                json.dumps(vector, ensure_ascii=False),
                str(vector.get("kind") or "sparse"),
                str(vector.get("backend") or BACKEND),
                signature,
            ),
        )
        encoded += 1
        if progress:
            progress(index, total)

    fp = source_fingerprint()
    vector_kind = "dense" if dense_enabled() else "sparse"
    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _set_meta(conn, "backend", BACKEND)
    _set_meta(conn, "vector_backend", BACKEND)
    _set_meta(conn, "vector_kind", vector_kind)
    _set_meta(conn, "updated_at", updated_at)
    _set_meta(conn, "source_fingerprint", fp["fingerprint"])
    conn.commit()
    chunks = int(conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"])
    cards = int(conn.execute(
        "SELECT COUNT(DISTINCT source_id) c FROM chunks WHERE source='knowledge'").fetchone()["c"])
    memory_chunks = int(conn.execute(
        "SELECT COUNT(*) c FROM chunks WHERE source='memory'").fetchone()["c"])
    conn.close()
    # 返回契约要和原来的 rebuild() 一致：rebuild 现在委托给这里，调用方读的还是那几个键。
    return {
        "ok": True, "changed": bool(encoded or removed),
        "encoded": encoded, "reused": reused, "removed": len(removed),
        "chunks": chunks, "dense": dense_enabled(),
        "backend": BACKEND, "index_backend": BACKEND, "vector_kind": vector_kind,
        "knowledge_cards": cards, "memory_chunks": memory_chunks,
        "sources": {"knowledge": cards, "memory": memory_chunks},
        "updated_at": updated_at,
        "db": str(db_path()), "source_fingerprint": fp["fingerprint"],
        "indexed_fingerprint": fp["fingerprint"],
        "embeddings": retrieval_embeddings.status(),
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
    rebuilt = sync_incremental()
    rebuilt["changed"] = True
    return rebuilt


def rebuild() -> dict[str, Any]:
    """整库重建。

    实现上就是"清空 + 增量同步"：以前这里另有一套写死列顺序的 INSERT，加了三列之后
    它就和表结构对不上了（`table chunks has 19 columns but 16 values were supplied`）。
    一份插入逻辑维护在一处，别再复制第二份。
    """
    conn = _conn()
    conn.execute("DELETE FROM chunks WHERE source IN ('knowledge', 'memory')")
    conn.commit()
    conn.close()
    result = sync_incremental()
    result["changed"] = True
    return result


def search(query: str, limit: int = 8, sources: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    # **绝不在这里重建**。搜索是热路径（提示词注入每条消息都走），而重建要把上千个
    # 分块重编一遍。索引缺失就直接返回空，让调用方退回词法，重建交给显式命令/定时任务。
    if not db_path().exists():
        return []
    # 查询侧要**按分块实际存的向量种类**来编码：`cosine` 在 kind 不一致时直接返 0，
    # 存了稠密却拿稀疏查询去比，等于整个索引静默失效。两种都按需编一次，各比各的。
    sparse_query = retrieval_embeddings.encode_sparse_query(q)
    dense_query = retrieval_embeddings.encode_query(q) if dense_enabled() else None
    if dense_query is not None and str(dense_query.get("kind")) != "dense":
        dense_query = None
    if not sparse_query and dense_query is None:
        return []
    wanted = _normal_sources(sources)
    conn = _conn()
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(f"SELECT * FROM chunks WHERE source IN ({placeholders})", wanted).fetchall()
    conn.close()
    hits = []
    terms = _query_terms(q)
    for row in rows:
        vec = retrieval_embeddings.decode(row["vector_json"])
        qvec = dense_query if str(vec.get("kind")) == "dense" else sparse_query
        if not qvec:
            continue
        vector_backend = str(qvec.get("backend") or BACKEND)
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
