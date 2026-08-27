"""P2：记忆的几道保护闸。

共同点是——它们都在保护"人"：用户手改的文件不被模型覆盖、用户的回答不被记忆写入
挤掉、用户亲口定的规矩不被反思改写、用久了不会悄悄变慢。
"""
from __future__ import annotations

import time

import pytest


# ── P2-2 核心记忆的外部漂移保护 ──────────────────────────────────────────────
def test_edit_refused_after_external_change(ivyea_home):
    """模型 view 之后、写入之前，文件被人改了 → 拒写 + 备份 + 让它重看。"""
    from ivyea_agent import memory_core
    memory_core.edit("user", "append", content="第一条")
    memory_core.view("user")                      # 模型看了一眼，记下基线
    p = memory_core.block_path("user")
    time.sleep(0.01)
    p.write_text(p.read_text(encoding="utf-8") + "- 用户手写的一条\n", encoding="utf-8")

    res = memory_core.edit("user", "append", content="模型基于旧内容的写入")
    assert res["ok"] is False
    assert res.get("drift") is True
    assert "用户手写的一条" in p.read_text(encoding="utf-8")     # 手写内容还在
    assert res.get("backup") and "bak" in res["backup"]


def test_edit_succeeds_after_re_viewing(ivyea_home):
    """按提示重新 view 之后就该能写 —— 保护不能变成死锁。"""
    from ivyea_agent import memory_core
    memory_core.edit("user", "append", content="第一条")
    memory_core.view("user")
    p = memory_core.block_path("user")
    time.sleep(0.01)
    p.write_text(p.read_text(encoding="utf-8") + "- 用户手写的一条\n", encoding="utf-8")
    memory_core.edit("user", "append", content="会被拒的")       # 触发漂移
    memory_core.view("user")                                     # 重新看
    res = memory_core.edit("user", "append", content="重看之后再写")
    assert res["ok"] is True
    body = p.read_text(encoding="utf-8")
    assert "用户手写的一条" in body and "重看之后再写" in body


def test_own_writes_are_not_mistaken_for_drift(ivyea_home):
    """连写两次不能把"我自己刚改的"当成外部漂移。"""
    from ivyea_agent import memory_core
    memory_core.view("user")
    assert memory_core.edit("user", "append", content="一")["ok"]
    assert memory_core.edit("user", "append", content="二")["ok"]


def test_fingerprint_catches_same_second_edits(ivyea_home):
    """只看 mtime 不够：同一秒内的两次写会看起来没变，所以要带内容 sha1。"""
    from ivyea_agent import memory_core
    p = memory_core.block_path("user")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("A", encoding="utf-8")
    fp1 = memory_core._fingerprint(p)
    import os
    p.write_text("B", encoding="utf-8")
    os.utime(p, ns=(fp1[0], fp1[0]))              # 把 mtime 强行改回去
    assert memory_core._fingerprint(p) != fp1     # 靠 sha1 认出来


# ── P2-3 记忆写入熔断 ────────────────────────────────────────────────────────
def _ctx(turn="t1"):
    from ivyea_agent.agent_tools import ToolContext
    c = ToolContext()
    c.turn_id = turn
    return c


def test_repeated_write_failures_trip_the_breaker(ivyea_home):
    """记忆是副作用，副作用失败绝不能吃掉用户的回答。"""
    from ivyea_agent import agent_tools
    ctx = _ctx()
    bad = {"operation": "update", "name": "不存在的记忆", "content": "x"}
    msgs = [agent_tools._t_memory_write(bad, ctx) for _ in range(3)]
    assert "别再重试了" in msgs[-1]
    assert "别再重试了" not in msgs[0]


def test_breaker_resets_on_success(ivyea_home):
    """数的是**连续**失败：中间成功一次就该清零。"""
    from ivyea_agent import agent_tools
    ctx = _ctx()
    bad = {"operation": "update", "name": "不存在的记忆", "content": "x"}
    agent_tools._t_memory_write(bad, ctx)
    agent_tools._t_memory_write(bad, ctx)
    ok = agent_tools._t_memory_write(
        {"operation": "add", "name": "新记忆", "category": "domain", "content": "正文"}, ctx)
    assert "别再重试" not in ok
    assert "别再重试了" not in agent_tools._t_memory_write(bad, ctx)


def test_breaker_resets_next_turn(ivyea_home):
    """熔断是"这一轮别再试了"，不是"这条会话永远别试了"。"""
    from ivyea_agent import agent_tools
    ctx = _ctx("turn-1")
    bad = {"operation": "update", "name": "不存在的记忆", "content": "x"}
    for _ in range(3):
        agent_tools._t_memory_write(bad, ctx)
    ctx.turn_id = "turn-2"
    assert "别再重试了" not in agent_tools._t_memory_write(bad, ctx)


def test_drift_rejection_does_not_count_toward_breaker(ivyea_home):
    """漂移拒绝说的是"重新 view 再写" —— 那正是我们希望它做的，不能把补救路堵死。"""
    from ivyea_agent import agent_tools, memory_core
    ctx = _ctx()
    memory_core.edit("user", "append", content="底稿")
    for _ in range(4):
        memory_core.view("user")
        p = memory_core.block_path("user")
        time.sleep(0.01)
        p.write_text(p.read_text(encoding="utf-8") + "- 又被手改了\n", encoding="utf-8")
        msg = agent_tools._t_core_memory_edit(
            {"block": "user", "operation": "append", "content": "写点什么"}, ctx)
    assert "别再重试了" not in msg
    assert "外部修改" in msg


# ── P2-5 情景记忆保留策略 ────────────────────────────────────────────────────
def test_prune_drops_old_dialogue_only(ivyea_home):
    """只收对话行。摘要/决策/巡检是提炼过的，而且决策是"上次已经否过"护栏的唯一数据源。"""
    from ivyea_agent import memory
    old_ts = time.time() - 400 * 86400
    conn = memory._conn()
    memory._index(conn, "[对话:user] 很久以前说的话", "", old_ts)
    memory._index(conn, "[决策] reject negative “旧词”", "", old_ts)
    memory._index(conn, "[会话摘要] 很久以前的摘要", "", old_ts)
    conn.commit(); conn.close()
    memory.index_turn("user", "今天说的话", "s")

    res = memory.prune_episodes(days=180)
    assert res["deleted"] == 1
    conn = memory._conn()
    rows = [r["text"] for r in conn.execute("SELECT text FROM search_fts").fetchall()]
    conn.close()
    assert not any("很久以前说的话" in t for t in rows)
    assert any("[决策]" in t for t in rows)
    assert any("[会话摘要]" in t for t in rows)
    assert any("今天说的话" in t for t in rows)


def test_prune_also_clears_the_token_sidecar(ivyea_home):
    """删 search_fts 不删 search_tok 会留下孤儿；更糟的是 FTS5 会复用 rowid，
    新行拿到旧 rowid 就继承了上一条的分词内容 —— 既不是孤儿也不是缺失，静默污染检索。
    """
    from ivyea_agent import memory
    old_ts = time.time() - 400 * 86400
    conn = memory._conn()
    memory._index(conn, "[对话:user] 陈年旧事", "", old_ts)
    conn.commit(); conn.close()
    # _TOK_OK 是**懒初始化**的：第一次 _conn() 之后才有值。在建连接之前读它
    # 永远拿到 None，于是这条用例会静默跳过 —— 而它测的恰恰是最容易出错的一段。
    if not memory._TOK_OK:
        pytest.skip("这套 SQLite 没有 FTS5，分词旁路索引本就不存在")
    memory.prune_episodes(days=180)
    conn = memory._conn()
    orphans = conn.execute(
        "SELECT COUNT(*) c FROM search_tok WHERE src NOT IN (SELECT rowid FROM search_fts)"
    ).fetchone()["c"]
    conn.close()
    assert orphans == 0


def test_prune_backs_up_the_db_before_first_delete(ivyea_home):
    """这是整套方案里唯一不可逆的一步，删之前必须留一份能回去的备份。"""
    from ivyea_agent import memory
    conn = memory._conn()
    memory._index(conn, "[对话:user] 陈年旧事", "", time.time() - 400 * 86400)
    conn.commit(); conn.close()
    res = memory.prune_episodes(days=180)
    assert res["deleted"] == 1
    assert (memory.DB_PATH.with_name(memory.DB_PATH.name + ".bak")).exists()


def test_dry_run_deletes_nothing(ivyea_home):
    from ivyea_agent import memory
    conn = memory._conn()
    memory._index(conn, "[对话:user] 陈年旧事", "", time.time() - 400 * 86400)
    conn.commit(); conn.close()
    res = memory.prune_episodes(days=180, dry_run=True)
    assert res["deleted"] == 0 and res["candidates"] == 1


def test_maybe_prune_is_throttled(ivyea_home):
    from ivyea_agent import config, memory
    memory.maybe_prune_episodes()
    config.set_setting(memory._PRUNE_TS_KEY, time.time())
    assert "今天已清理过" in memory.maybe_prune_episodes()["message"]


# ── P2-1 压缩前落记忆 ────────────────────────────────────────────────────────
def test_compact_feeds_the_summary_to_reflection(ivyea_home, monkeypatch):
    """复用刚生成的摘要，而不是把原始消息再喂一遍模型 —— 同一段对话不付两次钱。"""
    from ivyea_agent import context, memory_reflect
    seen = []
    monkeypatch.setattr(memory_reflect, "reflect_summary_async",
                        lambda text: seen.append(text) or True)

    class P:
        def complete(self, *a, **k):
            return "这次聊定了：广告预算按周调。"

    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(12)]
    _new, summary = context.compact(msgs, P(), keep_recent=0)
    assert summary
    assert seen == [summary]


def test_compact_survives_a_broken_reflection(ivyea_home, monkeypatch):
    """记忆是锦上添花，压缩绝不能因为它失败。"""
    from ivyea_agent import context, memory_reflect

    def boom(_text):
        raise RuntimeError("记忆层炸了")

    monkeypatch.setattr(memory_reflect, "reflect_summary_async", boom)

    class P:
        def complete(self, *a, **k):
            return "摘要"

    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(12)]
    new, summary = context.compact(msgs, P(), keep_recent=0)
    assert summary == "摘要" and len(new) < len(msgs)
