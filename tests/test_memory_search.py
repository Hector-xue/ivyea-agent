"""记忆检索单测：中文换说法能召回、标识符不串号、索引漂移能自愈。

历史坑（勿再犯）：
- FTS5 的 unicode61 把整段中文当一个 token，升级前只能靠 LIKE 子串兜底，换个说法就抓瞎；
- sync_markdown_index 会删掉 [档] 行再重灌，会在分词旁路索引里留下孤儿；
- 升级前写入的行没有分词索引，必须能被增量补齐，不能要求用户重装。
"""
from __future__ import annotations


def _texts(rows):
    return [r["text"] for r in rows]


def test_chinese_paraphrase_recall(ivyea_home):
    """核心收益：查询用词和记忆用词不同也要召回。这是升级前做不到的。"""
    from ivyea_agent import memory
    memory.remember("这个账号广告花费太高，ACoS 超标要降 bid")
    hits = memory.search("广告花钱太狠")
    assert any("广告花费太高" in t for t in _texts(hits))


def test_word_order_insensitive(ivyea_home):
    from ivyea_agent import memory
    memory.remember("预算分配以核心词优先")
    assert _texts(memory.search("分配预算"))


def test_asin_does_not_bleed(ivyea_home):
    """精确标识符必须精确命中——这正是向量检索做不到、必须靠词法兜住的一类。"""
    from ivyea_agent import memory
    memory.remember("库存只剩 12 天，暂停放量", asin="B08XYZ124")
    memory.remember("这个链接可以放量", asin="B08XYZ123")
    hits = memory.search("B08XYZ124")
    assert hits
    assert all(h["asin"] != "B08XYZ123" for h in hits)


def test_asin_field_is_searchable(ivyea_home):
    """按 ASIN 检索要能捞到该 ASIN 的行，哪怕正文里没重复写 ASIN。"""
    from ivyea_agent import memory
    memory.remember("这条链接对宽泛词一贯保守", asin="B0TEST999")
    assert _texts(memory.search("B0TEST999"))


def test_search_result_shape_unchanged(ivyea_home):
    """消费方契约：CLI/serve/retrieval 都按 text/asin/ts 消费，内部 score 不能泄漏出去。"""
    from ivyea_agent import memory
    memory.remember("形状测试")
    hits = memory.search("形状测试")
    assert hits
    assert set(hits[0].keys()) == {"rowid", "text", "asin", "ts"}


def test_empty_query_does_not_crash(ivyea_home):
    """空/纯标点查询会切出空 token，绝不能拼出非法 MATCH 把检索打挂。"""
    from ivyea_agent import memory
    memory.remember("随便一条")
    assert memory.search("") == []
    assert memory.search("，。！") == []


def test_rebuild_backfills_legacy_rows(ivyea_home):
    """老库升级：直接往 search_fts 塞行（绕过 _index，模拟升级前写入），rebuild 要能补齐。

    注意断言选的是**换词召回**而不是原词召回：原词能被 LIKE 子串兜底命中，
    无论分词索引在不在都为真，那样的断言测不出任何东西。
    """
    from ivyea_agent import memory
    conn = memory._conn()
    conn.execute("INSERT INTO search_fts (text, asin, ts) VALUES (?,?,?)",
                 ("[记忆] 宽泛批发类词一贯保守处理", "", 1.0))
    conn.commit()
    conn.close()
    assert not memory.search("保守批发词")        # 补齐前：只有 LIKE 兜底，换词召不回
    res = memory.rebuild_token_index()
    assert res["ok"] and res["added"] >= 1
    assert _texts(memory.search("保守批发词"))    # 补齐后：bigram 命中


def test_rowid_reuse_does_not_leave_stale_tokens(ivyea_home):
    """FTS5 删行后会复用 rowid：新行若拿到刚被删的 rowid，旧分词行既不算孤儿也不算缺失，
    会静默污染检索结果。这里直接构造该场景。"""
    from ivyea_agent import memory
    memory.remember("第一条内容讲的是库存周转")
    conn = memory._conn()
    conn.execute("DELETE FROM search_fts")
    conn.commit()
    conn.close()
    memory.remember("第二条内容讲的是广告出价")
    st = memory.stats()
    assert st["indexed"] == st["tokenized"]
    assert not memory.search("库存周转")          # 旧内容不能借着复用的 rowid 复活
    assert _texts(memory.search("广告出价"))


def test_rebuild_clears_orphans(ivyea_home):
    """源行被删后，旁路索引不能留下指向已消失 rowid 的孤儿。"""
    from ivyea_agent import memory
    memory.remember("会被删掉的一条")
    conn = memory._conn()
    conn.execute("DELETE FROM search_fts WHERE text LIKE '%会被删掉%'")
    conn.commit()
    conn.close()
    res = memory.rebuild_token_index()
    assert res["ok"] and res["removed"] >= 1
    assert not memory.search("会被删掉的一条")


def test_sync_markdown_index_keeps_token_index_aligned(ivyea_home):
    """sync_markdown_index 反复跑（每次 CLI 启动都跑）必须幂等，不能让索引越积越歪。"""
    from ivyea_agent import memory
    memory.note_path("").write_text("# 记忆\n\n- 宽泛批发类词一贯保守\n", encoding="utf-8")
    memory.sync_markdown_index()
    memory.sync_markdown_index()
    st = memory.stats()
    assert st["indexed"] == st["tokenized"]       # 无孤儿、无缺失
    assert _texts(memory.search("批发词保守"))


def test_stats_reports_segmentation(ivyea_home):
    from ivyea_agent import memory
    st = memory.stats()
    assert st["segmented_search"] is True
    assert "indexed" in st and "tokenized" in st
