"""记忆的语义层：向量缓存 + 词法/向量混合排序。

**为什么是"缓存"而不是"索引"**：这套记忆系统吃过索引漂移的大亏——内容进了文件却没进
索引，recall 抓瞎。所以这里的向量表按**内容哈希**做主键：文本一改，哈希就变，旧向量
自然失配、当场重算。不存在"索引和内容不一致"这种状态，也就不需要任何同步/重建仪式。

**为什么是 RRF 融合**：词法分（BM25，负数、量纲随语料变）和向量分（余弦，0~1）根本不可比，
硬加权就得反复调那个权重。RRF 只用**排名**不用分数：score = Σ 1/(k + rank)。
不需要调参、对量纲免疫，是混合检索的默认解。

**降级契约（重要）**：没配 dense 后端时，本模块必须让检索行为和纯词法时**完全一致**。
语义是增益，不是前置条件——ivyea-agent 是 pip 装的 CLI，不能因为用户没配 embedding
就退化。所以 `hybrid_rank` 在拿不到 dense 向量时直接原样返回词法顺序。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import config, retrieval_embeddings

# RRF 的平滑常数。文献里的通用默认是 60，但那是为 TREC 那种**上千条**结果的榜单调的：
# 1/(60+1) 和 1/(60+3) 只差 3%，在我们这种几十条的候选池上，融合会被压得几乎不起作用。
# 取 10 让前几名之间拉开足够差距，语义强命中才有可能压过一个弱的词法第一名——
# 而我们的词法信号本来就只是 token 重合度，粗糙，不该给它过大的话语权。
# 这是个调参选择，阶段 2 的评测框架会用真实问答对回归验证它。
RRF_K = 10


def _rrf_k() -> int:
    try:
        return max(1, int(config.get_setting("memory_rrf_k", RRF_K)))
    except (TypeError, ValueError):
        return RRF_K

# 单次检索最多现算多少条文档向量。缓存未命中时要真发请求/真跑模型，
# 不设上限会让"第一次搜索"卡到用户以为死机了。剩下的下次搜索再补。
MAX_EMBED_PER_CALL = 32


def db_path():
    return config.IVYEA_DIR / "memory.db"


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS mem_vec (
        text_hash TEXT NOT NULL,
        backend   TEXT NOT NULL,
        model     TEXT NOT NULL,
        payload   TEXT NOT NULL,
        ts        REAL,
        PRIMARY KEY (text_hash, backend, model))""")
    return conn


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def backend_key() -> Tuple[str, str, bool]:
    """(backend, model, 是否 dense)。dense=False 时整个语义层静默让路。"""
    st = retrieval_embeddings.status()
    active = str(st.get("active_backend") or "")
    dense = bool(st.get("semantic_enabled"))
    model = str(st.get("api_model") if active == retrieval_embeddings.API_BACKEND else st.get("model") or "")
    return active, model, dense


def _cached(conn, hashes: Sequence[str], backend: str, model: str) -> Dict[str, List[float]]:
    if not hashes:
        return {}
    out: Dict[str, List[float]] = {}
    # 分批 IN 查询：SQLite 的变量上限是 999，语料再大也不能一次塞进去
    for i in range(0, len(hashes), 400):
        chunk = list(hashes[i:i + 400])
        qs = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT text_hash, payload FROM mem_vec "
            f"WHERE backend=? AND model=? AND text_hash IN ({qs})",
            (backend, model, *chunk)).fetchall()
        for r in rows:
            try:
                vec = json.loads(r["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(vec, list) and vec:
                out[r["text_hash"]] = [float(v) for v in vec]
    return out


def _store(conn, text_hash: str, backend: str, model: str, values: List[float]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO mem_vec (text_hash, backend, model, payload, ts) VALUES (?,?,?,?,?)",
        (text_hash, backend, model, json.dumps(values), time.time()))


def _dense_values(payload: Dict[str, Any]) -> Optional[List[float]]:
    """从 retrieval_embeddings 的返回里取 dense 向量；它降级成 hash 时返回 None。"""
    if not isinstance(payload, dict) or payload.get("kind") != "dense":
        return None
    values = payload.get("values")
    if isinstance(values, list) and values:
        return [float(v) for v in values]
    return None


def embed_texts(texts: Sequence[str], *, budget: int = MAX_EMBED_PER_CALL) -> Dict[str, List[float]]:
    """取一批文本的向量（缓存优先）。返回 {text_hash: vector}，拿不到的键直接缺席。"""
    backend, model, dense = backend_key()
    if not dense or not texts:
        return {}
    hashes = [_hash(t) for t in texts]
    conn = _conn()
    try:
        vectors = _cached(conn, hashes, backend, model)
        missing = [(h, t) for h, t in zip(hashes, texts) if h not in vectors]
        for text_hash, text in missing[:budget]:
            payload = retrieval_embeddings.encode_document(text)
            values = _dense_values(payload)
            if values is None:
                # 后端临时降级（网络/额度）——这一轮就当没有语义，别把 hash 稀疏向量
                # 当成 dense 存进缓存，否则后面会拿它跟真 dense 向量算余弦，纯属噪音。
                break
            _store(conn, text_hash, backend, model, values)
            vectors[text_hash] = values
        conn.commit()
        return vectors
    finally:
        conn.close()


def embed_query(text: str) -> Optional[List[float]]:
    """查询向量。也走缓存——同一个问题反复问不该反复计费。"""
    backend, model, dense = backend_key()
    if not dense or not (text or "").strip():
        return None
    text_hash = _hash("q:" + text)
    conn = _conn()
    try:
        hit = _cached(conn, [text_hash], backend, model).get(text_hash)
        if hit:
            return hit
        values = _dense_values(retrieval_embeddings.encode_query(text))
        if values is None:
            return None
        _store(conn, text_hash, backend, model, values)
        conn.commit()
        return values
    finally:
        conn.close()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    if dot <= 0:
        return 0.0
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def hybrid_rank(query: str, items: List[Any], text_of: Callable[[Any], str],
                *, limit: int = 8) -> List[Any]:
    """把词法有序列表 `items` 与向量相似度用 RRF 融合，返回重排后的前 limit 条。

    `items` 必须**已经按词法相关度排好序**——它的下标就是词法排名。
    没有 dense 后端时原样返回前 limit 条，行为与纯词法完全一致。
    """
    if not items:
        return []
    _, _, dense = backend_key()
    if not dense:
        return items[:limit]
    qvec = embed_query(query)
    if not qvec:
        return items[:limit]

    texts = [text_of(it) for it in items]
    vectors = embed_texts(texts)
    if not vectors:
        return items[:limit]

    sims: List[Tuple[float, int]] = []
    for idx, text in enumerate(texts):
        vec = vectors.get(_hash(text))
        # 维度不一致 = 换过模型而缓存还是旧的：当作无向量处理，别算出一个假分数
        sims.append((_cosine(qvec, vec) if vec and len(vec) == len(qvec) else 0.0, idx))

    # 向量排名：只有真正命中语义（相似度 > 0）的才进榜，
    # 否则一堆 0 分文档会挤占 RRF 名次，把词法的正确结果压下去。
    #
    # 相似度先取整到 6 位再排序：数学上相等的余弦值（例如都等于 1/√3）在浮点里会因为
    # 运算次序不同而在末位有差异，不取整的话名次就由浮点噪音决定，同一个查询看起来
    # 时而这样排时而那样排，既没法复现也没法评测。取整后真同分就靠 idx 稳定破平，
    # 退化为保持词法原序。
    vec_order = [idx for score, idx in sorted(sims, key=lambda t: (-round(t[0], 6), t[1]))
                 if score > 0]
    vec_rank = {idx: rank for rank, idx in enumerate(vec_order)}

    k = _rrf_k()
    fused: List[Tuple[float, int]] = []
    for lex_rank, idx in enumerate(range(len(items))):
        score = 1.0 / (k + lex_rank + 1)
        if idx in vec_rank:
            score += 1.0 / (k + vec_rank[idx] + 1)
        fused.append((score, idx))
    fused.sort(key=lambda t: (-t[0], t[1]))   # 同分时保词法原序，结果稳定可复现
    return [items[idx] for _, idx in fused[:limit]]


def stats() -> Dict[str, Any]:
    backend, model, dense = backend_key()
    conn = _conn()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM mem_vec WHERE backend=? AND model=?",
                         (backend, model)).fetchone()["c"]
        total = conn.execute("SELECT COUNT(*) c FROM mem_vec").fetchone()["c"]
    finally:
        conn.close()
    return {"semantic": dense, "backend": backend, "model": model,
            "cached_vectors": n, "cached_total": total}


def clear_cache(*, all_backends: bool = False) -> int:
    """清向量缓存。换模型后用——缓存按 (hash, backend, model) 分区，其实不清也不会串，
    但换回旧模型时留着老向量反而省钱，所以默认只清当前后端。"""
    backend, model, _ = backend_key()
    conn = _conn()
    try:
        cur = (conn.execute("DELETE FROM mem_vec") if all_backends else
               conn.execute("DELETE FROM mem_vec WHERE backend=? AND model=?", (backend, model)))
        conn.commit()
        return max(0, cur.rowcount)
    finally:
        conn.close()
