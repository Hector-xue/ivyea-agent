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
    from . import memory_lock
    memory_lock.tune(conn)
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


# 进程内的语义总开关。**只给评测用**：评测要在同一份数据上跑"纯词法"和"混合"两遍
# 才能量出语义到底带来多少收益，而改 settings 会污染用户真实配置、也没法在一次进程里
# 来回切。生产代码路径永远不碰它。
_FORCE_LEXICAL = False


class lexical_only:
    """上下文管理器：临时强制纯词法。用 with 而不是全局设标志，
    保证异常路径也一定会还原——评测里漏还原会让后续所有查询静默降级。"""

    def __enter__(self):
        global _FORCE_LEXICAL
        self._prev = _FORCE_LEXICAL
        _FORCE_LEXICAL = True
        return self

    def __exit__(self, *exc):
        global _FORCE_LEXICAL
        _FORCE_LEXICAL = self._prev
        return False


def backend_key() -> Tuple[str, str, bool]:
    """(backend, model, 是否 dense)。dense=False 时整个语义层静默让路。"""
    st = retrieval_embeddings.status()
    active = str(st.get("active_backend") or "")
    dense = bool(st.get("semantic_enabled")) and not _FORCE_LEXICAL
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


# 明显垃圾的地板，**不是**精度旋钮。
#
# 实测（bge-small-zh-v1.5，口语化查询 vs 运营记忆）：**正确匹配的相似度只有 0.41~0.55**，
# 错误匹配也在 0.29~0.55。绝对阈值在这个压缩区间里根本分不开对错——最早按直觉设的 0.55
# 把所有结果静默杀光了。所以这里只留一个低地板挡明显无关的，真正的筛选交给
# "取前 N 名 + RRF 按排名融合"：排名对量纲和模型差异免疫，绝对分数不是。
MIN_SIMILARITY = 0.30

# 向量路最多贡献多少个候选。不设上限的话，一堆 0.3x 的底噪命中会挤满 RRF 名次，
# 把词法的正确结果压下去——语义应该是补召回，不是抢名次。
VEC_CANDIDATE_FACTOR = 3
VEC_CANDIDATE_MIN = 10


def _min_similarity() -> float:
    try:
        return float(config.get_setting("memory_min_similarity", MIN_SIMILARITY))
    except (TypeError, ValueError):
        return MIN_SIMILARITY


def vector_recall(query: str, items: List[Any], text_of: Callable[[Any], str],
                  *, budget: int = MAX_EMBED_PER_CALL, top_n: int = 0) -> List[int]:
    """**独立的**向量召回：对全部候选算余弦，返回按相似度降序的下标列表。

    注意这是一条独立召回路径，不是对词法结果的重排。早先的实现把语义做成"重排词法
    候选集"，结果词法一条都没召回时候选集是空的、语义根本没有机会——而那恰恰是语义
    检索唯一存在的理由（实测：口语化查询"东西要卖光了怎么办" vs 记忆"周转天数低于30天
    就下单"，词法零命中，重排式语义也跟着零命中）。
    """
    if not items:
        return []
    _, _, dense = backend_key()
    if not dense:
        return []
    qvec = embed_query(query)
    if not qvec:
        return []
    texts = [text_of(it) for it in items]
    vectors = embed_texts(texts, budget=budget)
    if not vectors:
        return []
    floor = _min_similarity()
    sims: List[Tuple[float, int]] = []
    for idx, text in enumerate(texts):
        vec = vectors.get(_hash(text))
        # 维度不一致 = 换过模型而缓存还是旧的：当作无向量处理，别算出一个假分数
        score = _cosine(qvec, vec) if vec and len(vec) == len(qvec) else 0.0
        if score >= floor:
            sims.append((score, idx))
    # 相似度先取整到 6 位再排序：数学上相等的余弦值在浮点里会因运算次序不同而末位有差异，
    # 不取整的话名次由浮点噪音决定，同一个查询时而这样排时而那样排，既没法复现也没法评测。
    sims.sort(key=lambda t: (-round(t[0], 6), t[1]))
    return [idx for _, idx in sims[:top_n]] if top_n > 0 else [idx for _, idx in sims]


def fuse(lex_ranked: Sequence[int], vec_ranked: Sequence[int], *, limit: int) -> List[int]:
    """RRF 融合两条召回路径的排名，返回融合后的下标列表。

    只用**排名**不用分数：BM25 分是负数且量纲随语料变，余弦是 0~1，两者硬加权就得
    反复调那个权重。RRF 对量纲免疫、免调参。
    """
    k = _rrf_k()
    scores: Dict[int, float] = {}
    order: Dict[int, int] = {}
    for rank, idx in enumerate(lex_ranked):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
        order.setdefault(idx, rank)
    for rank, idx in enumerate(vec_ranked):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
        # 词法没召回的项，用一个排在所有词法结果之后的序号参与破平，保证结果稳定
        order.setdefault(idx, len(lex_ranked) + rank)
    fused = sorted(scores.items(), key=lambda kv: (-kv[1], order.get(kv[0], 0)))
    return [idx for idx, _ in fused[:limit]]


def hybrid_rank(query: str, items: List[Any], text_of: Callable[[Any], str],
                *, limit: int = 8, lex_ranked: Optional[Sequence[int]] = None,
                budget: int = MAX_EMBED_PER_CALL) -> List[Any]:
    """双路召回 + RRF 融合。

    `lex_ranked` 是词法召回的下标排名；省略时视为"items 已按词法序排好且全部命中"
    （情景记忆那条路就是这样，SQL 已经排好序了）。
    没有 dense 后端时原样返回词法顺序的前 limit 条，与纯词法**逐条相同**。
    """
    if not items:
        return []
    lex = list(lex_ranked) if lex_ranked is not None else list(range(len(items)))
    _, _, dense = backend_key()
    if not dense:
        return [items[i] for i in lex[:limit]]
    vec = vector_recall(query, items, text_of, budget=budget,
                        top_n=max(limit * VEC_CANDIDATE_FACTOR, VEC_CANDIDATE_MIN))
    if not vec:
        return [items[i] for i in lex[:limit]]
    return [items[i] for i in fuse(lex, vec, limit=limit)]


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
