"""待定区（dual-buffer consolidation）：新洞察先留观，再入库。

动机是实测翻车：反思把"用户几次在开发后要求发版"总结成了"用户习惯开发完就发版"，
而真实规矩是**未经批准绝不发版**。这类错误概括统计上成立、证据门槛拦不住，
只能靠"先别当真"来兜。

最关键的一条断言：**待定记忆绝不能泄漏进正常检索和索引层**。
泄漏了的话待定区就白做了。
"""
from __future__ import annotations

import json
import time


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, *a, **kw):
        return json.dumps(self.payload, ensure_ascii=False)


def _seed_episodes(n, offset=0):
    from ivyea_agent import memory
    conn = memory._conn()
    now = time.time() + offset
    for i in range(n):
        memory._index(conn, f"[对话:user] 第{offset}-{i}条经历", "", now + i)
    conn.commit()
    conn.close()


def _op(name="推断偏好", evidence=3):
    return {"operation": "add", "name": name, "category": "feedback",
            "description": f"{name}的描述", "content": f"{name}的正文",
            "evidence_count": evidence}


def _reflect(name="推断偏好", evidence=3, offset=0):
    from ivyea_agent import config, memory_reflect
    _seed_episodes(20, offset)
    config.set_setting(memory_reflect._LAST_TS_KEY, 0.0)
    return memory_reflect.reflect(FakeProvider({"operations": [_op(name, evidence)]}))


# ── 隔离：这是待定区的立身之本 ──────────────────────────────────────────────
def test_pending_not_in_search(ivyea_home):
    from ivyea_agent import memory_store
    _reflect()
    assert memory_store.search("推断偏好") == []
    assert memory_store.search("推断偏好的描述") == []


def test_pending_not_in_index_digest(ivyea_home):
    """泄漏进索引层等于直接进模型上下文——待定区就完全失效了。"""
    from ivyea_agent import memory_store
    _reflect()
    assert "推断偏好" not in memory_store.index_digest()


def test_pending_dir_not_treated_as_category(ivyea_home):
    """.pending 在 memory/ 下，不排除的话会被当成一个分类扫进 list_entries。"""
    from ivyea_agent import memory_store
    _reflect()
    assert memory_store.list_entries() == []
    assert all(not e.category.startswith(".") for e in memory_store.list_pending())


def test_history_dir_not_treated_as_category(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="项", category="domain", description="d", content="v1")
    memory_store.apply("update", name="项", content="v2")
    assert [e.name for e in memory_store.list_entries()] == ["项"]


# ── 观察次数与自动转正 ──────────────────────────────────────────────────────
def test_first_sighting_stays_pending(ivyea_home):
    from ivyea_agent import memory_store
    res = _reflect()
    assert res["pending"] and not res["applied"]
    assert len(memory_store.list_pending()) == 1
    assert "待定" in res["message"]


def test_repeated_sightings_promote(ivyea_home):
    """跨多次反思还在得出同一结论，才算规律；这和"一批经历里有几条支撑"不是一回事。"""
    from ivyea_agent import memory_reflect, memory_store
    for i in range(memory_reflect.PROMOTE_AFTER_SIGHTINGS):
        res = _reflect(offset=i * 1000)
    assert res["applied"]
    assert memory_store.get("推断偏好") is not None
    assert memory_store.list_pending() == []


def test_auto_promoted_still_marked_inference(ivyea_home):
    """自动转正不等于人确认过——仍必须带推断标记。"""
    from ivyea_agent import memory_reflect, memory_store
    for i in range(memory_reflect.PROMOTE_AFTER_SIGHTINGS):
        _reflect(offset=i * 1000)
    e = memory_store.get("推断偏好")
    assert e.source == "reflection" and e.uncertain


def test_confidence_grows_with_sightings(ivyea_home):
    from ivyea_agent import memory_store
    _reflect()
    first = memory_store.get_pending("推断偏好").confidence
    _reflect(offset=1000)
    assert memory_store.get_pending("推断偏好").confidence > first


# ── 人工确认 ────────────────────────────────────────────────────────────────
def test_user_confirm_promotes_to_full_trust(ivyea_home):
    """只有人点头这一条路径能越过不确定线。"""
    from ivyea_agent import memory_store
    _reflect()
    res = memory_store.promote_pending("推断偏好", confirmed_by_user=True)
    assert res["ok"]
    e = memory_store.get("推断偏好")
    assert e.source == "user" and e.confidence == 1.0 and not e.uncertain
    assert memory_store.list_pending() == []


def test_reject_discards(ivyea_home):
    from ivyea_agent import memory_store
    _reflect()
    assert memory_store.reject_pending("推断偏好")["ok"]
    assert memory_store.list_pending() == []
    assert memory_store.get("推断偏好") is None


def test_promote_missing(ivyea_home):
    from ivyea_agent import memory_store
    assert not memory_store.promote_pending("不存在")["ok"]
    assert not memory_store.reject_pending("不存在")["ok"]


def test_failed_promotion_keeps_pending(ivyea_home):
    """升级撞上查重时不能把待定记忆也丢了，否则这条洞察凭空消失。

    顺序很重要：必须先有待定记忆、之后才出现同名正式记忆。反过来的话反思压根不会
    进待定区（已存在同名记忆时走的是 update 分支），测不到这个场景。
    """
    from ivyea_agent import memory_store
    memory_store.add_pending("推断偏好", "待定的正文", category="feedback", description="待定的")
    memory_store.apply("add", name="推断偏好", category="feedback",
                       description="已存在的", content="已存在的正文")
    res = memory_store.promote_pending("推断偏好")
    assert not res["ok"]
    assert memory_store.get_pending("推断偏好") is not None    # 还在


def test_update_of_existing_memory_bypasses_pending(ivyea_home):
    """update 是对已有记忆的修正，本来就有依据，不该被留观拦住。"""
    from ivyea_agent import config, memory_reflect, memory_store
    memory_store.apply("add", name="已有项", category="feedback",
                       description="旧描述", content="旧正文")
    _seed_episodes(20)
    config.set_setting(memory_reflect._LAST_TS_KEY, 0.0)
    res = memory_reflect.reflect(FakeProvider({"operations": [{
        "operation": "update", "name": "已有项", "content": "新正文", "evidence_count": 1}]}))
    assert res["applied"]
    assert "新正文" in memory_store.get("已有项").body


def test_pending_carries_evidence(ivyea_home):
    from ivyea_agent import memory_store
    _reflect()
    assert "支撑" in memory_store.get_pending("推断偏好").evidence


def test_add_pending_requires_content(ivyea_home):
    from ivyea_agent import memory_store
    assert not memory_store.add_pending("", "x")["ok"]
    assert not memory_store.add_pending("名字", "")["ok"]
