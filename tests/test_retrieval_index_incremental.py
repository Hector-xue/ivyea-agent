"""检索索引：增量同步、热路径不重建、稠密档开关。"""
from __future__ import annotations

import pytest


@pytest.fixture()
def index(ivyea_home):
    from ivyea_agent import retrieval_index
    return retrieval_index


def test_second_sync_reuses_everything(index):
    """增量的全部意义：没变的分块不重编。

    原来是"整张表删掉重来"，改一个字也要把上千个分块全部重新编码——稀疏下只是浪费，
    稠密下就是每次改动卡几分钟，这才是稠密向量一直上不了的真正原因。
    """
    first = index.sync_incremental()
    assert first["encoded"] > 0
    second = index.sync_incremental()
    assert second["encoded"] == 0
    assert second["reused"] == first["encoded"]
    assert second["changed"] is False


def test_changed_card_only_reencodes_its_own_chunks(index, monkeypatch):
    """改一张卡，只该重编这张卡的分块，别的原样留着。"""
    index.sync_incremental()
    from ivyea_agent import knowledge

    real_get_card = knowledge.get_card

    def patched(card_id):
        card = real_get_card(card_id)
        if card and card_id == "listing.product_detail_page":
            card = {**card, "body": str(card.get("body") or "") + "\n新增一句用于触发重编。"}
        return card

    monkeypatch.setattr(index.knowledge, "get_card", patched)
    result = index.sync_incremental()
    assert result["encoded"] > 0
    # 绝大多数分块必须是复用的，否则等于又退回全量重建
    assert result["reused"] > result["encoded"] * 5


def test_removed_source_drops_its_chunks(index, monkeypatch):
    index.sync_incremental()
    before = index.status()["chunks"]
    real_list = index.knowledge.list_cards

    monkeypatch.setattr(index.knowledge, "list_cards", lambda: real_list()[:5])
    result = index.sync_incremental()
    assert result["removed"] > 0
    assert index.status()["chunks"] < before


def test_search_never_rebuilds_on_hot_path(index, monkeypatch):
    """搜索是热路径（提示词注入每条消息都走），绝不能在里面重建索引。"""
    called = {"n": 0}

    def boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("search 不该触发重建")

    monkeypatch.setattr(index, "rebuild", boom)
    monkeypatch.setattr(index, "sync_incremental", boom)
    # 索引还没建过：应当直接返回空，而不是当场重建
    assert index.search("广告花了钱不出单", 5) == []
    assert called["n"] == 0


def test_dense_is_off_by_default(index):
    """默认必须是关的：打开后第一次同步要把全部分块过一遍模型。

    那种一次性开销不该藏在用户随便一条命令里发生。
    """
    assert index.dense_enabled() is False
    result = index.sync_incremental()
    assert result["dense"] is False


def test_dense_toggle_changes_signature(index):
    """换档位要让签名变，否则开了稠密也不会重编，等于开关无效。"""
    from ivyea_agent import config

    sparse_sig = index._vector_signature("some chunk text")
    config.set_setting(index.DENSE_SETTING, True)
    if not index.dense_enabled():
        pytest.skip("本机没有可用的 dense 后端")
    assert index._vector_signature("some chunk text") != sparse_sig


def test_query_encoding_follows_stored_vector_kind(index):
    """查询侧必须按分块实际存的种类编码。

    `cosine` 在 kind 不一致时直接返 0——存了稠密却拿稀疏查询去比，整个索引会**静默**
    失效：不报错、只是永远没有命中。
    """
    index.sync_incremental()
    hits = index.search("negative targeting search term", 5, sources=("knowledge",))
    assert hits
    assert all(hit.get("vector_score", 0) > 0 for hit in hits)
