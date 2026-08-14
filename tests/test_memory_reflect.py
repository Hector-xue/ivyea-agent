"""反思/巩固单测。用假 provider，绝不打真实 LLM——测的是门槛和落盘逻辑，不是模型。

两道闸门是这个模块的全部价值，必须测死：
- 显著性门槛：经历不够就别烧 LLM 调用；
- 证据门槛：只被提到一次的东西不准固化成"长期规律"（防错误信念固化）。
"""
from __future__ import annotations

import json
import time


class FakeProvider:
    """记录被调用的 prompt，并按预置脚本返回。"""

    def __init__(self, payload, *, raw: str = ""):
        self.payload = payload
        self.raw = raw
        self.calls = []

    def complete(self, system, user, json_mode=False, temperature=0.2, timeout=60.0):
        self.calls.append({"system": system, "user": user})
        return self.raw or json.dumps(self.payload, ensure_ascii=False)


class BoomProvider:
    def complete(self, *a, **kw):
        raise RuntimeError("模型挂了")


def _seed_episodes(n, prefix="[对话:user] "):
    from ivyea_agent import memory
    conn = memory._conn()
    now = time.time()
    for i in range(n):
        memory._index(conn, f"{prefix}第{i}条经历：广告花费又超了", "", now + i)
    conn.commit()
    conn.close()


def _op(name="宽泛词打法", operation="add", evidence=3, category="domain"):
    return {"operation": operation, "name": name, "category": category,
            "description": "这个账号对宽泛批发类词一贯保守",
            "keywords": "否词,宽泛词", "content": "6/7/8 月连续否掉宽泛批发类词，建议默认否。",
            "evidence_count": evidence}


def test_significance_gate_blocks_when_too_few(ivyea_home):
    """经历不够就不该调 LLM——这是省钱也是省时间的那道闸门。"""
    from ivyea_agent import memory_reflect
    _seed_episodes(3)
    p = FakeProvider({"operations": [_op()]})
    res = memory_reflect.reflect(p)
    assert res["ok"] and not res["applied"]
    assert p.calls == []                       # 一次模型调用都没发生
    assert "不足" in res["message"]


def test_force_bypasses_significance_gate(ivyea_home):
    from ivyea_agent import memory_reflect, memory_store
    _seed_episodes(3)
    p = FakeProvider({"operations": [_op()]})
    res = memory_reflect.reflect(p, force=True)
    # 阶段 7 起新洞察先进待定区（先留观再入库），所以看 pending 而不是 applied
    assert res["ok"] and res["pending"]
    assert memory_store.get_pending("宽泛词打法") is not None


def test_evidence_gate_drops_single_support_insight(ivyea_home):
    """只被提到一次的东西不准落盘——防止一句口误被固化成长期偏好。"""
    from ivyea_agent import memory_reflect, memory_store
    _seed_episodes(20)
    p = FakeProvider({"operations": [_op(evidence=1)]})
    res = memory_reflect.reflect(p)
    assert res["ok"] and not res["applied"]
    assert "门槛" in res["skipped"][0]
    assert memory_store.get("宽泛词打法") is None


def test_evidence_gate_does_not_block_update(ivyea_home):
    """update 是对已有记忆的修正，本来就有历史依据；用证据数卡它会让过时记忆改不掉。"""
    from ivyea_agent import memory_reflect, memory_store
    memory_store.apply("add", name="宽泛词打法", category="domain",
                       description="旧描述", content="旧结论")
    _seed_episodes(20)
    p = FakeProvider({"operations": [_op(operation="update", evidence=1)]})
    res = memory_reflect.reflect(p)
    assert res["applied"]
    assert "建议默认否" in memory_store.get("宽泛词打法").body


def test_reflection_runs_and_persists(ivyea_home):
    from ivyea_agent import memory_reflect, memory_store
    _seed_episodes(20)
    p = FakeProvider({"operations": [_op()]})
    res = memory_reflect.reflect(p)
    assert res["ok"] and len(res["pending"]) == 1
    e = memory_store.get_pending("宽泛词打法")
    assert e.category == "domain" and "默认否" in e.body


def test_existing_index_is_given_to_model(ivyea_home):
    """必须把现有记忆索引喂给模型，否则它没法判断该 update 哪条 → 必然碎片化。"""
    from ivyea_agent import memory_reflect, memory_store
    memory_store.apply("add", name="已有记忆", category="project",
                       description="已经记过的事", content="正文")
    _seed_episodes(20)
    p = FakeProvider({"operations": []})
    memory_reflect.reflect(p)
    assert "[project/已有记忆]" in p.calls[0]["user"]


def test_watermark_advances_even_when_nothing_applied(ivyea_home):
    """一条都没落盘也要推进水位线，否则下次会把同一批经历再嚼一遍。"""
    from ivyea_agent import memory_reflect
    _seed_episodes(20)
    assert memory_reflect.last_reflect_ts() == 0.0
    memory_reflect.reflect(FakeProvider({"operations": []}))
    assert memory_reflect.last_reflect_ts() > 0
    assert memory_reflect.pending() == []


def test_archive_rows_are_not_episodes(ivyea_home):
    """[档] 是策展 markdown 的派生副本，不是新经历——拿它反思等于把结论再嚼一遍。"""
    from ivyea_agent import memory, memory_reflect
    conn = memory._conn()
    for i in range(20):
        memory._index(conn, f"[档]  已经沉淀过的第{i}条", "", time.time() + i)
    conn.commit()
    conn.close()
    assert memory_reflect.pending() == []
    assert not memory_reflect.should_reflect()


def test_json_in_code_fence_is_parsed(ivyea_home):
    """模型爱把 JSON 包在 ```json 里，不能因此整批丢掉。"""
    from ivyea_agent import memory_reflect, memory_store
    _seed_episodes(20)
    body = json.dumps({"operations": [_op()]}, ensure_ascii=False)
    p = FakeProvider(None, raw=f"好的，我提炼了以下内容：\n```json\n{body}\n```\n以上。")
    res = memory_reflect.reflect(p)
    assert res["pending"]
    assert memory_store.get_pending("宽泛词打法") is not None


def test_garbage_output_fails_soft(ivyea_home):
    from ivyea_agent import memory_reflect
    _seed_episodes(20)
    res = memory_reflect.reflect(FakeProvider(None, raw="我拒绝回答"))
    assert not res["ok"] and "JSON" in res["message"]


def test_provider_error_fails_soft(ivyea_home):
    """反思挂掉绝不能把主流程带崩——它是锦上添花，不是关键路径。"""
    from ivyea_agent import memory_reflect
    _seed_episodes(20)
    res = memory_reflect.reflect(BoomProvider())
    assert not res["ok"] and "失败" in res["message"]


def test_malformed_operations_are_ignored(ivyea_home):
    from ivyea_agent import memory_reflect
    _seed_episodes(20)
    p = FakeProvider({"operations": ["不是字典", {"operation": "bogus", "name": "x"}, 42]})
    res = memory_reflect.reflect(p)
    assert res["ok"]


def test_auto_disabled_by_setting(ivyea_home):
    from ivyea_agent import config, memory_reflect
    _seed_episodes(50)
    config.set_setting("memory_auto_reflect", False)
    assert not memory_reflect.should_reflect()


def test_status_shape(ivyea_home):
    from ivyea_agent import memory_reflect
    _seed_episodes(20)
    st = memory_reflect.status()
    assert st["ready"] is True
    assert st["pending_episodes"] == 20
    assert st["last_reflect"] == "从未"
