"""溯源与置信。

动机来自一次真实翻车：反思把"用户几次在开发后要求发版"总结成了"用户习惯开发完就发版"，
而真实规矩是**未经批准绝不发版**。证据门槛挡不住这类——统计上确实发生过，
只是"发生过"不等于"是偏好"。

所以要做两件事：把推断和用户原话**区分开**，以及能回答"你凭什么这么认为"。
"""
from __future__ import annotations

import json
import time


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, *a, **kw):
        return json.dumps(self.payload, ensure_ascii=False)


def _seed_episodes(n):
    from ivyea_agent import memory
    conn = memory._conn()
    now = time.time()
    for i in range(n):
        memory._index(conn, f"[对话:user] 第{i}条经历", "", now + i)
    conn.commit()
    conn.close()


# ── 默认与兼容 ──────────────────────────────────────────────────────────────
def test_user_written_memory_is_fully_trusted(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="红线", category="domain", description="d", content="x")
    e = memory_store.get("红线")
    assert e.source == "user" and e.confidence == 1.0 and not e.uncertain


def test_legacy_file_defaults_to_trusted(ivyea_home):
    """老文件没有 source/confidence 字段，必须当作可信——不能升级一下全变成"推断"。"""
    from ivyea_agent import memory_store
    d = ivyea_home / "memory" / "domain"
    d.mkdir(parents=True)
    (d / "老的.md").write_text("---\nname: 老的\ncategory: domain\n---\n\n正文\n", encoding="utf-8")
    e = memory_store.get("老的")
    assert e.source == "user" and e.confidence == 1.0


def test_broken_confidence_falls_back(ivyea_home):
    from ivyea_agent import memory_store
    assert memory_store._clamp_conf("不是数字", "reflection") == 0.5
    assert memory_store._clamp_conf("5", "user") == 1.0      # 超范围要夹住
    assert memory_store._clamp_conf("-1", "user") == 0.0


# ── 反思产出被标记为推断 ────────────────────────────────────────────────────
def test_reflection_output_marked_as_inference(ivyea_home):
    from ivyea_agent import memory_reflect, memory_store
    _seed_episodes(20)
    memory_reflect.reflect(FakeProvider({"operations": [{
        "operation": "add", "name": "推断出来的偏好", "category": "feedback",
        "description": "d", "content": "内容", "evidence_count": 3}]}))
    # 阶段 7 起，反思的新洞察先落待定区（先留观再入库），所以在这里取
    e = memory_store.get_pending("推断出来的偏好")
    assert e.source == "reflection"
    assert e.uncertain                       # 必须被标成不确定
    assert e.evidence and "支撑" in e.evidence


def test_confidence_grows_with_evidence(ivyea_home):
    """证据越多越可信，但永远不该和用户原话一样确信。"""
    from ivyea_agent import memory_reflect, memory_store
    _seed_episodes(20)

    def _make(name, ev):
        memory_reflect.reflect(FakeProvider({"operations": [{
            "operation": "add", "name": name, "category": "feedback",
            "description": f"描述{name}", "content": f"内容{name}", "evidence_count": ev}]}))
        return memory_store.get_pending(name)

    low = _make("弱证据", 2)
    from ivyea_agent import config
    config.set_setting(memory_reflect._LAST_TS_KEY, 0.0)   # 重置水位线好跑第二次
    high = _make("强证据完全不同的主题内容", 8)
    assert high.confidence > low.confidence
    assert high.confidence <= memory_store.REFLECTION_MAX_CONFIDENCE   # 封顶在不确定线以下
    assert high.uncertain                     # 证据再多，未经人确认仍是推断


def test_uncertain_marked_in_index_digest(ivyea_home):
    """推断必须当着模型的面标出来，否则它会把"我猜的"和"你说的"一视同仁地执行。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="推断项", category="feedback", description="描述",
                       content="x", source="reflection", confidence=0.5)
    assert "推断" in memory_store.index_digest()


def test_confirmed_memory_not_downgraded_by_reflection(ivyea_home):
    """你亲口说过的规则，不该因为反思又推断了一遍就变成"推断"。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="品牌词红线", category="domain",
                       description="品牌词处理", content="品牌词永远不否")
    memory_store.apply("update", name="品牌词红线", content="品牌词永远不否（补充说明）",
                       source="reflection", confidence=0.5)
    e = memory_store.get("品牌词红线")
    assert e.source == "user" and e.confidence == 1.0


def test_plain_update_keeps_confidence(ivyea_home):
    """普通更新不该悄悄改动置信度。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="推断项", category="feedback", description="d",
                       content="v1", source="reflection", confidence=0.6)
    memory_store.apply("update", name="推断项", content="v2")
    assert memory_store.get("推断项").confidence == 0.6


# ── 证据可追溯 ──────────────────────────────────────────────────────────────
def test_evidence_records_range_not_every_id(ivyea_home):
    """逐条存 id 会让 frontmatter 比正文还长；时间范围+区间已够人去核对。"""
    from ivyea_agent import memory_reflect
    rows = [{"rowid": 10, "ts": time.time() - 86400}, {"rowid": 42, "ts": time.time()}]
    note = memory_reflect._evidence_note(3, rows)
    assert "3 条支撑" in note and "#10-42" in note


def test_evidence_note_empty_rows(ivyea_home):
    from ivyea_agent import memory_reflect
    assert memory_reflect._evidence_note(3, []) == ""


def test_to_dict_exposes_provenance(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="项", category="domain", description="d", content="x",
                       source="reflection", confidence=0.4, evidence="2 条支撑")
    d = memory_store.get("项").to_dict()
    assert d["source"] == "reflection" and d["confidence"] == 0.4 and d["evidence"] == "2 条支撑"
