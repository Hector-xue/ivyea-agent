"""遗忘与容量治理。

核心命题：索引层的名额要给"最近还在用的"，不是"最近才写的"。只按时间截断的话，
一条每周都用的核心打法会被一条半年前的一次性结论挤掉——这正是记忆系统用到
第三、四个月开始劣化的方式。

另一条硬约束：**绝不静默删除**。降级判错的代价是少看一眼，删除判错是永久丢失。
"""
from __future__ import annotations

import time

DAY = 86400.0


def _mk(memory_store, name, **kw):
    memory_store.apply("add", name=name, category=kw.pop("category", "domain"),
                       description=kw.pop("description", f"{name}的描述"),
                       content=kw.pop("content", f"{name}的正文"), **kw)
    return memory_store.get(name)


def _touch(memory_decay, entry, *, hits=1, days_ago=0.0, first_days_ago=None):
    """直接写使用统计，模拟"这条记忆多久前被用过几次"。"""
    conn = memory_decay._conn()
    now = time.time()
    conn.execute("INSERT OR REPLACE INTO mem_usage (key, hits, last_hit, first_seen, pinned) "
                 "VALUES (?,?,?,?,COALESCE((SELECT pinned FROM mem_usage WHERE key=?),0))",
                 (memory_decay._key(entry.category, entry.name), hits,
                  (now - days_ago * DAY) if hits else None,
                  now - (first_days_ago if first_days_ago is not None else days_ago + 60) * DAY,
                  memory_decay._key(entry.category, entry.name)))
    conn.commit()
    conn.close()


# ── 打分 ────────────────────────────────────────────────────────────────────
def test_recent_beats_old(ivyea_home):
    from ivyea_agent import memory_decay, memory_store
    a = _mk(memory_store, "最近用过")
    b = _mk(memory_store, "很久没用")
    _touch(memory_decay, a, hits=3, days_ago=1)
    _touch(memory_decay, b, hits=3, days_ago=180)
    stats = memory_decay.usage()
    assert memory_decay.score(a, stats)["score"] > memory_decay.score(b, stats)["score"]


def test_frequent_beats_rare(ivyea_home):
    from ivyea_agent import memory_decay, memory_store
    a = _mk(memory_store, "常用", description="库存周转与备货", content="补货节奏")
    b = _mk(memory_store, "偶尔", description="广告竞价调整", content="降低出价")
    _touch(memory_decay, a, hits=20, days_ago=5)
    _touch(memory_decay, b, hits=1, days_ago=5)
    stats = memory_decay.usage()
    assert memory_decay.score(a, stats)["score"] > memory_decay.score(b, stats)["score"]


def test_frequency_is_log_compressed(ivyea_home):
    """第 1 次到第 5 次的差别，远比第 50 次到第 55 次重要。"""
    from ivyea_agent import memory_decay
    d_low = memory_decay._frequency(5) - memory_decay._frequency(1)
    d_high = memory_decay._frequency(55) - memory_decay._frequency(50)
    assert d_low > d_high


def test_halflife_behaviour(ivyea_home):
    from ivyea_agent import memory_decay
    now = time.time()
    fresh = memory_decay._recency(now, now)
    half = memory_decay._recency(now - memory_decay.HALFLIFE_DAYS * DAY, now)
    assert fresh == 1.0
    assert abs(half - 0.5) < 0.01
    assert memory_decay._recency(None, now) == 0.0


def test_confidence_has_lowest_weight(ivyea_home):
    """低置信但天天用得上的记忆，不该输给高置信但没人用的。"""
    from ivyea_agent import memory_decay, memory_store
    hot = _mk(memory_store, "低置信但常用", description="库存周转与备货",
              content="补货节奏", source="reflection", confidence=0.4)
    cold = _mk(memory_store, "高置信但冷门", description="广告竞价调整", content="降低出价")
    _touch(memory_decay, hot, hits=15, days_ago=1)
    _touch(memory_decay, cold, hits=0, days_ago=200, first_days_ago=300)
    stats = memory_decay.usage()
    assert memory_decay.score(hot, stats)["score"] > memory_decay.score(cold, stats)["score"]


# ── 保护与钉住 ──────────────────────────────────────────────────────────────
def test_new_memory_protected_by_grace_period(ivyea_home):
    """刚记下来还没机会被召回，不能因为"从没用过"就判低分踢出去。"""
    from ivyea_agent import memory_decay, memory_store
    e = _mk(memory_store, "刚记的")
    s = memory_decay.score(e, memory_decay.usage())
    assert s["keep"] and s["fresh"] and s["reason"] == "保护期内"


def test_old_unused_memory_archived(ivyea_home):
    from ivyea_agent import memory_decay, memory_store
    e = _mk(memory_store, "老且没人用")
    _touch(memory_decay, e, hits=0, days_ago=300, first_days_ago=400)
    s = memory_decay.score(e, memory_decay.usage())
    assert not s["keep"] and "降级" in s["reason"]


def test_pinned_never_archived(ivyea_home):
    """红线规则平时不会被问起，可正因如此才更不能忘。"""
    from ivyea_agent import memory_decay, memory_store
    e = _mk(memory_store, "红线规则")
    _touch(memory_decay, e, hits=0, days_ago=999, first_days_ago=1000)
    assert not memory_decay.score(e, memory_decay.usage())["keep"]
    memory_decay.set_pinned(e.category, e.name, True)
    s = memory_decay.score(e, memory_decay.usage())
    assert s["keep"] and s["pinned"] and s["reason"] == "钉住"


def test_unpin_works(ivyea_home):
    from ivyea_agent import memory_decay, memory_store
    e = _mk(memory_store, "项")
    memory_decay.set_pinned(e.category, e.name, True)
    memory_decay.set_pinned(e.category, e.name, False)
    assert not memory_decay.usage(e.category, e.name)["pinned"]


# ── 索引层与检索 ────────────────────────────────────────────────────────────
def test_archived_entry_leaves_index_but_stays_searchable(ivyea_home):
    """降级 ≠ 删除：不再常驻上下文，但检索仍要能找到。这是整个设计的安全底线。"""
    from ivyea_agent import memory_decay, memory_store
    e = _mk(memory_store, "冷门打法", description="冷门的关键词")
    _touch(memory_decay, e, hits=0, days_ago=300, first_days_ago=400)
    assert "冷门打法" not in memory_store.index_digest()
    assert "未列出" in memory_store.index_digest()
    assert [h["name"] for h in memory_store.search("冷门的关键词")] == ["冷门打法"]


def test_search_records_hits(ivyea_home):
    """使用统计是遗忘打分的唯一数据来源，检索必须真的在记。"""
    from ivyea_agent import memory_decay, memory_store
    _mk(memory_store, "被搜的", description="独特关键词")
    memory_store.search("独特关键词")
    memory_store.search("独特关键词")
    u = memory_decay.usage("domain", "被搜的")
    assert u["hits"] == 2 and u["last_hit"]


def test_hit_recording_failure_does_not_break_search(ivyea_home, monkeypatch):
    """统计挂了绝不能影响检索本身——它是辅助数据，不是关键路径。"""
    from ivyea_agent import memory_decay, memory_store
    _mk(memory_store, "项", description="关键词")
    monkeypatch.setattr(memory_decay, "_conn",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert memory_store.search("关键词")


def test_index_prefers_active_over_recent(ivyea_home):
    """核心命题：索引名额给"最近还在用的"，不是"最近才写的"。"""
    from ivyea_agent import memory_decay, memory_store
    hot = _mk(memory_store, "每周都用的打法", description="库存周转与备货", content="补货节奏")
    cold = _mk(memory_store, "写得晚但没人用", description="广告竞价调整", content="降低出价")
    _touch(memory_decay, hot, hits=30, days_ago=1, first_days_ago=200)
    _touch(memory_decay, cold, hits=0, days_ago=100, first_days_ago=100)
    digest = memory_store.index_digest()
    assert "每周都用的打法" in digest
    assert "写得晚但没人用" not in digest


def test_report_shape(ivyea_home):
    from ivyea_agent import memory_decay, memory_store
    _mk(memory_store, "甲")
    rep = memory_decay.report(memory_store.list_entries())
    assert rep["total"] == 1 and rep["active"] == 1
    assert rep["rows"][0]["name"] == "甲"


def test_ranking_is_stable(ivyea_home):
    """同分要按名字稳定破平，否则索引层每次组装顺序都不一样，没法排查。"""
    from ivyea_agent import memory_decay, memory_store
    for n in ("丙", "甲", "乙"):
        _mk(memory_store, n)
    a = [e.name for e, _ in memory_decay.rank(memory_store.list_entries())]
    b = [e.name for e, _ in memory_decay.rank(memory_store.list_entries())]
    assert a == b
