"""随包 embedding 模型的守卫测试。

盯的是**会静默坏掉**的那几件事：分词错位、截断长度被改小、语义分辨力消失、降级不干净。
它们坏了功能不报错，只会让检索悄悄变差——所以必须有断言压住。
"""
from __future__ import annotations

import math

import pytest

from ivyea_agent import onnx_embedding, retrieval_embeddings, wordpiece


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


@pytest.fixture(scope="module")
def model():
    if not onnx_embedding.available():
        pytest.skip(f"内置模型不可用：{onnx_embedding.unavailable_reason()}")
    return onnx_embedding


def test_model_is_bundled_and_loads(model):
    info = model.info()
    assert info["available"] is True
    assert info["dimensions"] == 512
    assert info["source_model"] == "BAAI/bge-small-zh-v1.5"
    # 体积是产品约束：进 wheel 的东西不能失控。真超了要么换配方要么明确决定放宽。
    assert info["size_bytes"] < 40_000_000


def test_max_seq_length_is_512(model):
    """**512 不能改小**。曾经按 256 截断做评测，把长文档砍掉一半，结论直接反了——
    当时以为"int8 量化掉点严重"，其实是截断的锅。改回 512 后 int8 和 fp32 基本持平。
    """
    assert onnx_embedding.MAX_SEQ == 512
    assert model.info()["max_seq_length"] == 512


def test_vectors_are_unit_length(model):
    """归一化编在 ONNX 图里，Python 侧不再算一遍——所以这条也是在守那张图没被换错。"""
    v = model.encode("广告预算怎么分配")
    assert v is not None
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-4)


def test_paraphrases_beat_unrelated(model):
    """核心断言：换个说法要比无关话题更近。这正是语义检索存在的理由——
    下面这些例子词法上是零重合的。

    用相对比较而不是绝对阈值：实测同一模型上正确匹配 0.41~0.58、错配 0.29~0.55，
    绝对分界线根本不存在，卡阈值会在换模型时莫名其妙全部失效。
    """
    pairs = [
        ("广告怎么优化预算", "推广的钱该怎么分配", "日本站的税务申报"),
        ("高点击没有订单怎么办", "点击很多但是不出单", "FBA 入仓的箱子尺寸要求"),
        ("主图点击率太低", "首图不吸引人怎么改", "账户被封了怎么申诉"),
    ]
    for anchor, near, far in pairs:
        a, n, f = model.encode(anchor), model.encode(near), model.encode(far)
        assert _cos(a, n) > _cos(a, f), f"{anchor!r}: 同义 {_cos(a, n):.3f} 未超过无关 {_cos(a, f):.3f}"


def test_batch_matches_single(model):
    """批量和单条必须**逐位一致**。

    这不是形式主义：int8 动态量化的激活 scale 按整个张量现算，真按批走的话同一段文本
    和不同邻居凑一批会算出不同向量（实测余弦 0.99，等长同批也一样，不是 padding 的问题）。
    后果很隐蔽——缓存里的向量和重算的对不上，重建索引结果就变。所以 encode_many
    内部是逐条编码的，这条测试就是钉住这个决定，别谁看到"批量能快 1.2×"又改回去。
    """
    texts = ["广告", "高点击零单要不要否词", "这是一段比较长的中文文本" * 20]
    batched = model.encode_many(texts)
    for text, vec in zip(texts, batched):
        single = model.encode(text)
        assert vec is not None and single is not None
        assert _cos(vec, single) > 0.99999, f"{text[:20]!r} 批量与单条不一致"


def test_empty_input_is_none(model):
    """切不出 token 的输入必须返回 None，不能返回零向量。
    零向量和任何东西余弦都是 0，混进候选池就是一条永远排在中间的噪音。"""
    assert model.encode("") is None
    assert model.encode("   ") is None
    assert model.encode_many(["", "广告"])[0] is None


def test_tokenizer_ids_are_in_vocab(model):
    from ivyea_agent.onnx_embedding import _load
    vocab = _load()["vocab"]
    toks = wordpiece.tokenize("广告 ACoS 45% 的 B08XYZ123", vocab)
    assert toks
    assert not [t for t in toks if t not in vocab]
    assert "广" in toks and "告" in toks


def test_backend_defaults_to_builtin(monkeypatch):
    """默认必须是自带语义，而不是当年的 hash 稀疏向量。"""
    monkeypatch.setattr(retrieval_embeddings.config, "get_setting",
                        lambda key, default=None: default)
    st = retrieval_embeddings.status()
    assert st["configured_backend"] == retrieval_embeddings.BUILTIN_BACKEND
    if st["builtin_ready"]:
        assert st["active_backend"] == retrieval_embeddings.BUILTIN_BACKEND
        assert st["semantic_enabled"] is True
        assert st["vector_kind"] == "dense"


def test_legacy_hash_setting_migrates_to_builtin():
    """老部署 settings.json 里躺着的 "hash" 要跟着走到 builtin。

    hash 是当年的**默认值**不是谁的选择；留在 hash 等于升级完还是没有语义。
    关语义要写 "sparse"，它不能被规范化成 "hash"——否则会被当成老默认值迁回来，关不掉。
    """
    assert retrieval_embeddings._normal_backend("hash") == retrieval_embeddings.BUILTIN_BACKEND
    assert retrieval_embeddings._normal_backend("") == retrieval_embeddings.BUILTIN_BACKEND
    assert retrieval_embeddings._normal_backend("sparse") == retrieval_embeddings.SPARSE_BACKEND
    assert retrieval_embeddings._normal_backend("off") == retrieval_embeddings.SPARSE_BACKEND


def test_encode_document_returns_dense_payload(model):
    payload = retrieval_embeddings.encode_document("否词的判断标准是什么")
    if payload["backend"] == retrieval_embeddings.BUILTIN_BACKEND:
        assert payload["kind"] == "dense"
        assert len(payload["values"]) == model.dimensions()


def test_missing_model_degrades_quietly(monkeypatch, tmp_path):
    """模型读不到时必须安静退回稀疏，而不是把检索整个弄崩。"""
    monkeypatch.setattr(onnx_embedding, "_STATE", None)
    monkeypatch.setattr(onnx_embedding, "_FAILED", "")
    monkeypatch.setattr(onnx_embedding, "model_path", lambda: tmp_path / "nope.onnx")
    assert onnx_embedding.available() is False
    assert onnx_embedding.encode("广告") is None
    assert "缺失" in onnx_embedding.unavailable_reason()
    monkeypatch.setattr(onnx_embedding, "_STATE", None)
    monkeypatch.setattr(onnx_embedding, "_FAILED", "")
