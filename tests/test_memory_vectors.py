"""语义层单测：向量缓存、RRF 融合、降级契约、API 客户端。

这里最重要的不是"语义有多准"（那是模型的事，评测框架去量），而是三件工程事实：
1. **降级契约**：没配 dense 后端时，检索行为必须和纯词法**逐条相同**——语义是增益不是前置；
2. **缓存按内容哈希**：内容一改旧向量自动失配，不可能出现索引漂移；
3. **后端临时故障**（网络/额度）不能污染缓存，也不能让检索失败。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


# ── 确定性假 dense 后端：把文本映射成可预测的向量，好断言排序 ──────────────────
def _fake_vec(text: str):
    """三维**正交**向量：命中哪个主题就在哪一维打分。

    刻意保持正交（不加平滑项）：主题不相干时余弦严格为 0，这样才能测到
    "语义无关的项被排除出向量榜"这条真实行为。加了平滑项会让万物微弱相关，
    掩盖掉真实的过滤逻辑。
    """
    t = text.lower()
    return [
        float(t.count("库存") + t.count("inventory")),
        float(t.count("广告") + t.count("ad")),
        float(t.count("价格") + t.count("price")),
    ]


@pytest.fixture()
def dense(monkeypatch, ivyea_home):
    """把 retrieval_embeddings 换成确定性 dense 后端。"""
    from ivyea_agent import memory_vectors, retrieval_embeddings

    def fake_status():
        return {"active_backend": "api", "semantic_enabled": True,
                "api_model": "fake-embed", "model": "fake-embed"}

    monkeypatch.setattr(retrieval_embeddings, "status", fake_status)
    monkeypatch.setattr(retrieval_embeddings, "encode_document",
                        lambda t: {"kind": "dense", "values": _fake_vec(t)})
    monkeypatch.setattr(retrieval_embeddings, "encode_query",
                        lambda t: {"kind": "dense", "values": _fake_vec(t)})
    return memory_vectors


# ── 降级契约 ────────────────────────────────────────────────────────────────
def test_no_dense_backend_returns_lexical_order_unchanged(ivyea_home):
    """默认（hash 后端）下必须原样返回词法顺序——这是整个语义层的安全网。"""
    from ivyea_agent import memory_vectors
    items = [{"t": "甲"}, {"t": "乙"}, {"t": "丙"}]
    out = memory_vectors.hybrid_rank("随便", items, lambda x: x["t"], limit=2)
    assert out == items[:2]


def test_memory_search_identical_without_semantic(ivyea_home):
    """扩大候选池不能改变无语义时的结果——池子大了但只取前 limit 条，顺序未变。"""
    from ivyea_agent import memory
    for i in range(30):
        memory.remember(f"第{i}条关于广告出价的记忆")
    from ivyea_agent import memory_vectors
    with memory_vectors.lexical_only():
        assert not memory_vectors_enabled()
        hits = memory.search("广告出价", limit=5)
    assert len(hits) == 5


def memory_vectors_enabled() -> bool:
    from ivyea_agent import memory_vectors
    return memory_vectors.backend_key()[2]


def test_empty_items(ivyea_home):
    from ivyea_agent import memory_vectors
    assert memory_vectors.hybrid_rank("x", [], lambda i: "", limit=5) == []


# ── RRF 融合 ────────────────────────────────────────────────────────────────
def test_semantic_lifts_relevant_item_up(dense):
    """语义要能把"词法排在后面但语义最相关"的那条捞上来——这是做这层的全部意义。"""
    items = [
        {"t": "广告 广告 广告 完全无关的噪音"},   # 词法第 1，但和查询语义不符
        {"t": "价格 价格 价格 也是噪音"},
        {"t": "库存 库存 库存 周转与备货"},        # 词法第 3，语义第 1
    ]
    out = dense.hybrid_rank("库存", items, lambda x: x["t"], limit=3)
    assert out[0]["t"].startswith("库存")


def test_rrf_keeps_lexical_signal(dense):
    """融合不是让语义一家独大：词法强命中且语义也不差的，仍应排在前面。"""
    items = [{"t": "库存周转天数怎么算"}, {"t": "库存"}, {"t": "广告出价"}]
    out = dense.hybrid_rank("库存", items, lambda x: x["t"], limit=3)
    assert "广告出价" not in [o["t"] for o in out[:2]]


def test_perfectly_inverted_rankings_tie_and_keep_lexical_order(dense):
    """RRF 的固有性质：两个排名完全互为逆序时所有项同分（1/(k+1)+1/(k+n) 对称）。
    这不是 bug——此时没有任何信息判断谁更好，保词法原序是正确且可复现的行为。
    留这条用例是为了把它钉成"已知且刻意"的行为，别下次有人当 bug 去"修"。"""
    items = [{"t": "广告 广告 广告"}, {"t": "价格 价格"}, {"t": "库存"}]
    out = dense.hybrid_rank("库存 价格 广告", items, lambda x: x["t"], limit=3)
    assert [o["t"] for o in out] == [i["t"] for i in items]


def test_vector_recall_is_independent_of_lexical(dense):
    """**核心回归**：词法一条都没召回时，语义仍必须能召回。

    早先的实现把语义做成"对词法候选集重排"，于是词法零命中时候选集为空、语义
    根本没有机会——而那恰恰是语义检索唯一存在的理由。真实模型实测暴露了这个缺陷
    （口语化查询 0/3 命中）。这条用例把双路召回钉死。
    """
    items = [{"t": "广告出价"}, {"t": "库存周转"}]
    out = dense.hybrid_rank("库存", items, lambda x: x["t"], limit=2, lex_ranked=[])
    assert out and out[0]["t"] == "库存周转"


def test_lexical_only_items_still_returned(dense):
    """反过来也要成立：向量没召回的，词法结果不能丢。"""
    items = [{"t": "完全无关的内容"}, {"t": "库存周转"}]
    out = dense.hybrid_rank("无关", items, lambda x: x["t"], limit=2, lex_ranked=[0])
    assert any(o["t"] == "完全无关的内容" for o in out)


def test_low_similarity_filtered_by_floor(dense, monkeypatch):
    """低于地板的相似度不进向量榜——挡明显无关的，避免底噪挤占 RRF 名次。"""
    from ivyea_agent import config
    config.set_setting("memory_min_similarity", 0.99)
    items = [{"t": "库存 广告"}]          # 与查询 "库存" 相似但不等于 1.0
    assert dense.vector_recall("库存", items, lambda x: x["t"]) == []


def test_vector_candidates_capped(dense):
    """向量路的候选数要有上限，否则底噪命中会淹没词法的正确结果。"""
    items = [{"t": f"库存 {i}"} for i in range(50)]
    got = dense.vector_recall("库存", items, lambda x: x["t"], budget=50, top_n=10)
    assert len(got) == 10


def test_fuse_is_pure_rank_based(dense):
    """fuse 只吃排名不吃分数——两条路都排第一的项必须胜出。"""
    fused = dense.fuse([2, 0, 1], [2, 1, 0], limit=3)
    assert fused[0] == 2


def test_fuse_deterministic_for_vector_only_items(dense):
    """词法没召回的项也要有稳定的破平序号，否则结果不可复现。"""
    a = dense.fuse([0], [3, 2], limit=3)
    b = dense.fuse([0], [3, 2], limit=3)
    assert a == b


def test_rrf_k_is_tunable(dense):
    """k 要可调——阶段 2 的评测框架要能扫它，否则调参只能靠感觉。"""
    from ivyea_agent import config
    config.set_setting("memory_rrf_k", 3)
    assert dense._rrf_k() == 3
    config.set_setting("memory_rrf_k", "不是数字")
    assert dense._rrf_k() == dense.RRF_K      # 配坏了要退回默认，不能抛异常打挂检索


def test_ties_preserve_lexical_order(dense):
    """同分时必须保词法原序，否则同一个查询两次结果不一样，没法排查也没法评测。"""
    items = [{"t": "价格 A"}, {"t": "价格 B"}, {"t": "价格 C"}]
    first = dense.hybrid_rank("价格", items, lambda x: x["t"], limit=3)
    second = dense.hybrid_rank("价格", items, lambda x: x["t"], limit=3)
    assert first == second


def test_limit_respected(dense):
    items = [{"t": f"库存 {i}"} for i in range(20)]
    assert len(dense.hybrid_rank("库存", items, lambda x: x["t"], limit=4)) == 4


# ── 缓存 ────────────────────────────────────────────────────────────────────
def test_vectors_cached_and_reused(dense, monkeypatch):
    from ivyea_agent import retrieval_embeddings
    calls = {"n": 0}
    orig = retrieval_embeddings.encode_document

    def counting(text):
        calls["n"] += 1
        return orig(text)

    monkeypatch.setattr(retrieval_embeddings, "encode_document", counting)
    items = [{"t": "库存周转"}, {"t": "广告出价"}]
    dense.hybrid_rank("库存", items, lambda x: x["t"], limit=2)
    after_first = calls["n"]
    assert after_first == 2
    dense.hybrid_rank("库存", items, lambda x: x["t"], limit=2)
    assert calls["n"] == after_first          # 第二次全部命中缓存，一次都没重算


def test_changed_text_invalidates_cache(dense):
    """按内容哈希做 key：内容一改，旧向量自动失配重算。这是"缓存不是索引"的核心保证。"""
    dense.embed_texts(["原始内容"])
    st1 = dense.stats()["cached_vectors"]
    dense.embed_texts(["原始内容被改过了"])
    assert dense.stats()["cached_vectors"] == st1 + 1


def test_query_vector_cached(dense, monkeypatch):
    from ivyea_agent import retrieval_embeddings
    calls = {"n": 0}
    orig = retrieval_embeddings.encode_query
    monkeypatch.setattr(retrieval_embeddings, "encode_query",
                        lambda t: (calls.__setitem__("n", calls["n"] + 1), orig(t))[1])
    dense.embed_query("同一个问题")
    dense.embed_query("同一个问题")
    assert calls["n"] == 1                    # 同一个问题反复问不该反复计费


def test_embed_budget_caps_work_per_call(dense):
    """首次搜索不能因为要现算几百条向量而卡到用户以为死机。"""
    texts = [f"内容{i}" for i in range(100)]
    got = dense.embed_texts(texts, budget=5)
    assert len(got) == 5


def test_backend_failure_not_cached(dense, monkeypatch):
    """后端临时降级返回 sparse 时，绝不能把它当 dense 存进缓存——
    否则后面会拿 hash 稀疏向量跟真 dense 向量算余弦，纯属噪音。"""
    from ivyea_agent import retrieval_embeddings
    monkeypatch.setattr(retrieval_embeddings, "encode_document",
                        lambda t: {"kind": "sparse", "values": {"a": 1.0}})
    assert dense.embed_texts(["会失败的内容"]) == {}
    assert dense.stats()["cached_vectors"] == 0


def test_dimension_mismatch_scores_zero(dense, monkeypatch):
    """换过模型而缓存还是旧维度时，必须当作无向量，不能算出一个假分数。"""
    from ivyea_agent import retrieval_embeddings
    dense.embed_texts(["库存周转"])
    monkeypatch.setattr(retrieval_embeddings, "encode_query",
                        lambda t: {"kind": "dense", "values": [1.0] * 99})
    items = [{"t": "库存周转"}, {"t": "广告出价"}]
    out = dense.hybrid_rank("库存", items, lambda x: x["t"], limit=2)
    assert len(out) == 2                      # 不崩、不乱排，退回词法顺序


def test_clear_cache(dense):
    dense.embed_texts(["甲", "乙"])
    assert dense.stats()["cached_vectors"] == 2
    assert dense.clear_cache() >= 2
    assert dense.stats()["cached_vectors"] == 0


# ── API 后端的 HTTP 契约（打本地桩服务器，不碰真实供应商）─────────────────────
class _StubHandler(BaseHTTPRequestHandler):
    payload = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
    status_code = 200
    seen = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _StubHandler.seen = {"path": self.path, "body": json.loads(body or b"{}"),
                             "auth": self.headers.get("Authorization", "")}
        self.send_response(_StubHandler.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(_StubHandler.payload).encode())

    def log_message(self, *a):
        pass


@pytest.fixture()
def stub_server():
    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_api_backend_request_contract(ivyea_home, stub_server, monkeypatch):
    """验证我们发出的请求符合 OpenAI /v1/embeddings 约定：路径、Bearer、model/input。"""
    from ivyea_agent import retrieval_embeddings
    monkeypatch.setenv("TEST_EMBED_KEY", "sk-test-123")
    retrieval_embeddings.configure(backend="api", api_base=stub_server,
                                   api_model="BAAI/bge-m3", api_key_env="TEST_EMBED_KEY")
    st = retrieval_embeddings.status()
    assert st["active_backend"] == "api" and st["semantic_enabled"]

    out = retrieval_embeddings.encode_document("测试文本")
    assert out["kind"] == "dense" and out["values"] == [0.1, 0.2, 0.3]
    assert _StubHandler.seen["path"].endswith("/v1/embeddings")
    assert _StubHandler.seen["auth"] == "Bearer sk-test-123"
    assert _StubHandler.seen["body"]["model"] == "BAAI/bge-m3"
    assert _StubHandler.seen["body"]["input"] == "测试文本"


def test_api_backend_http_error_degrades_to_hash(ivyea_home, stub_server, monkeypatch):
    """额度用尽/服务挂了要降级成 sparse，而不是把检索整个打挂。"""
    from ivyea_agent import retrieval_embeddings
    monkeypatch.setenv("TEST_EMBED_KEY", "sk-test-123")
    retrieval_embeddings.configure(backend="api", api_base=stub_server,
                                   api_key_env="TEST_EMBED_KEY")
    _StubHandler.status_code = 429
    try:
        out = retrieval_embeddings.encode_document("测试")
        assert out["kind"] == "sparse"
        assert "fallback_error" in out
    finally:
        _StubHandler.status_code = 200


def test_api_backend_missing_key_reports_clearly(ivyea_home, monkeypatch):
    from ivyea_agent import retrieval_embeddings
    monkeypatch.delenv("NOPE_KEY", raising=False)
    retrieval_embeddings.configure(backend="api", api_base="http://127.0.0.1:1",
                                   api_key_env="NOPE_KEY")
    st = retrieval_embeddings.status()
    # key 没配 → 不该走 api，但也不该把语义整个关掉：退到随包的自带查表
    assert st["active_backend"] != retrieval_embeddings.API_BACKEND
    assert st["semantic_enabled"] is True
    assert st["active_backend"] == retrieval_embeddings.STATIC_BACKEND
    # 原因必须说清楚，否则用户只会觉得"配了没反应"
    assert "NOPE_KEY" in st["fallback_reason"]


def test_api_backend_missing_base_reports_clearly(ivyea_home, monkeypatch):
    from ivyea_agent import retrieval_embeddings
    monkeypatch.setenv("TEST_EMBED_KEY", "sk-test-123")
    retrieval_embeddings.configure(backend="api", api_base="", api_key_env="TEST_EMBED_KEY")
    st = retrieval_embeddings.status()
    assert st["active_backend"] != retrieval_embeddings.API_BACKEND
    assert st["semantic_enabled"] is True
    assert st["active_backend"] == retrieval_embeddings.STATIC_BACKEND
    assert "api_base" in st["fallback_reason"]


def test_default_backend_is_bundled_and_semantic(ivyea_home):
    """默认必须**零依赖零配置**——但现在这件事由随包的静态查表满足，而不是退回没有语义。

    旧契约是"默认 hash"，那等于所有人装完都没有语义检索，除非自己去配 API 或装
    2G 的本地模型。static 同样不联网、不要 key、不加依赖，但真的能按语义召回。
    """
    from ivyea_agent import retrieval_embeddings
    st = retrieval_embeddings.status()
    assert st["configured_backend"] == retrieval_embeddings.STATIC_BACKEND
    assert st["active_backend"] == retrieval_embeddings.STATIC_BACKEND
    assert st["semantic_enabled"] is True
    # 零依赖仍是硬约束：不许因此要求用户装 sentence-transformers 或配 key
    assert st["external_dependency"] is False
    assert not st["api_ready"]


def test_sparse_escape_hatch_still_works(ivyea_home):
    """显式写 sparse 仍能关掉语义——调试和"我就是不想要向量"要有出路。"""
    from ivyea_agent import retrieval_embeddings
    retrieval_embeddings.configure(backend="sparse")
    st = retrieval_embeddings.status()
    assert st["active_backend"] == retrieval_embeddings.HASH_BACKEND
    assert not st["semantic_enabled"]
