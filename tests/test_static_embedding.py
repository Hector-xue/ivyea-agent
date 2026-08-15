"""自带静态语义向量表的守卫测试。

这里盯的是**会静默坏掉**的那几件事：查表错位、语义分辨力消失、降级不干净。
它们坏了功能不会报错，只会让检索悄悄变差——所以必须有断言压住。
"""
from __future__ import annotations

import math

import pytest

from ivyea_agent import retrieval_embeddings, static_embedding, wordpiece


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


@pytest.fixture(scope="module")
def table():
    if not static_embedding.available():
        pytest.skip(f"静态向量表不可用：{static_embedding.unavailable_reason()}")
    return static_embedding


def test_table_is_bundled_and_loads(table):
    info = table.info()
    assert info["available"] is True
    assert info["vocab_size"] > 20000
    assert info["dimensions"] >= 128
    # 体积是产品约束：进 wheel 的东西不能失控。真超了要么换配方要么明确决定放宽。
    assert info["size_bytes"] < 12_000_000


def test_vectors_are_unit_length(table):
    v = table.encode("广告预算怎么分配")
    assert v is not None
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-5)


def test_paraphrases_beat_unrelated(table):
    """核心断言：换个说法要比无关话题更近。

    这正是语义检索存在的理由——词法在这些例子上是零重合的。
    用相对比较而不是绝对阈值：静态嵌入的绝对相似度没有可靠的分界线
    （真模型上都测过：正确匹配 0.41~0.55、错配 0.29~0.55），
    卡绝对值会在换配方时莫名其妙地全部失效。
    """
    pairs = [
        ("广告怎么优化预算", "推广的钱该怎么分配", "日本站的税务申报"),
        ("高点击没有订单怎么办", "点击很多但是不出单", "FBA 入仓的箱子尺寸要求"),
        ("主图点击率太低", "首图不吸引人怎么改", "账户被封了怎么申诉"),
    ]
    for anchor, near, far in pairs:
        a, n, f = table.encode(anchor), table.encode(near), table.encode(far)
        assert _cos(a, n) > _cos(a, f), f"{anchor!r}: 同义 {_cos(a, n):.3f} 未超过无关 {_cos(a, f):.3f}"


def test_empty_and_punctuation_input_is_none(table):
    """切不出 token 的输入必须返回 None，不能返回零向量。

    零向量会和任何东西的余弦都是 0，混进候选池就是一条永远排在中间的噪音。
    """
    assert table.encode("") is None
    assert table.encode("   ") is None


def test_tokenizer_matches_vocab_ids(table):
    """分词结果必须能在词表里查到 —— 查不到就是整体错位（历史上踩过 splitlines 的坑）。"""
    from ivyea_agent.static_embedding import _load
    vocab = _load()["vocab"]
    toks = wordpiece.tokenize("广告 ACoS 45% 的 B08XYZ123", vocab)
    assert toks, "切不出 token"
    unknown = [t for t in toks if t not in vocab]
    assert not unknown, f"词表里没有这些 token：{unknown}"
    # 中文该按字切开，而不是整段变成一个 [UNK]
    assert "广" in toks and "告" in toks


def test_backend_defaults_to_static(monkeypatch):
    """默认必须是自带语义，而不是当年的 hash 稀疏向量。"""
    monkeypatch.setattr(retrieval_embeddings.config, "get_setting",
                        lambda key, default=None: default)
    st = retrieval_embeddings.status()
    assert st["configured_backend"] == retrieval_embeddings.STATIC_BACKEND
    if st["static_ready"]:
        assert st["active_backend"] == retrieval_embeddings.STATIC_BACKEND
        assert st["semantic_enabled"] is True
        assert st["vector_kind"] == "dense"


def test_legacy_hash_setting_migrates_to_static():
    """老部署 settings.json 里躺着的 "hash" 要跟着走到 static。

    hash 是当年的**默认值**不是谁的选择；留在 hash 等于升级完还是没有语义。
    真想要稀疏的人写 "sparse"，那条路必须仍然通。
    """
    assert retrieval_embeddings._normal_backend("hash") == retrieval_embeddings.STATIC_BACKEND
    assert retrieval_embeddings._normal_backend("") == retrieval_embeddings.STATIC_BACKEND
    # 关语义要写 "sparse"。它**不能**被规范化成 "hash"——那个值会被当成老默认值
    # 迁移回 static，结果就是关不掉。
    assert retrieval_embeddings._normal_backend("sparse") == retrieval_embeddings.SPARSE_BACKEND
    assert retrieval_embeddings._normal_backend("off") == retrieval_embeddings.SPARSE_BACKEND


def test_encode_document_returns_dense_payload(table):
    payload = retrieval_embeddings.encode_document("否词的判断标准是什么")
    if payload["backend"] == retrieval_embeddings.STATIC_BACKEND:
        assert payload["kind"] == "dense"
        assert len(payload["values"]) == table.dimensions()


def test_missing_table_degrades_quietly(monkeypatch, tmp_path):
    """表读不到时必须安静退回稀疏，而不是把检索整个弄崩。"""
    monkeypatch.setattr(static_embedding, "_STATE", None)
    monkeypatch.setattr(static_embedding, "_FAILED", "")
    monkeypatch.setattr(static_embedding, "table_path", lambda: tmp_path / "nope.npz")
    assert static_embedding.available() is False
    assert static_embedding.encode("广告") is None
    assert "缺失" in static_embedding.unavailable_reason()
    # 缓存要清干净，免得污染同进程后续用例
    monkeypatch.setattr(static_embedding, "_STATE", None)
    monkeypatch.setattr(static_embedding, "_FAILED", "")
