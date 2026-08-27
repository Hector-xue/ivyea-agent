"""serve 侧的记忆接线：注入 / opt-out / 情景记录。

**这个文件存在的理由**：记忆的算法层一直是好的，坏的是 serve 这条路上一行记忆代码
都没有——用户在 IvyeaOps、飞书、任务台里聊天时，模型根本不知道记忆库里有什么。
下面每一条用例都对应一个"如果哪天有人把它改回去，就又变成没记忆"的点。
"""
from __future__ import annotations

import pytest


def _chat_route():
    """真的闲聊路由 —— 用真 Route 而不是替身，免得哪天 Route 长出新字段测试还在过。"""
    from ivyea_agent.routing import Route
    return Route(lane="chat")


@pytest.fixture()
def wired(ivyea_home, monkeypatch):
    """备好一条核心记忆 + 一条分类记忆，并掐掉知识检索（它要读知识库、与本文件无关）。"""
    from ivyea_agent import knowledge, memory_core, memory_store, service
    memory_core.edit("user", "append", content="用户叫 Hector，汇报一律用中文。")
    memory_store.apply("add", name="领星广告方法论", content="规则引擎 + LLM 复核。",
                       category="domain", description="领星广告优化怎么做")
    monkeypatch.setattr(knowledge, "evidence_context",
                        lambda *a, **k: {"text": "", "citations": [], "should_retrieve": False})
    return service


def _system_of(service, payload, ctx, route=None):
    messages, _created, _base = service._chat_messages(
        str(payload.get("message") or "x"), payload, ctx, route)
    return str(messages[0].get("content") or "")


def _ctx(service, **kw):
    from ivyea_agent.agent_tools import ToolContext
    kw.setdefault("plan_mode", True)
    return ToolContext(**kw)


def test_serve_system_prompt_carries_core_and_index(wired):
    """serve 的 system 里必须同时有核心记忆和分类记忆索引 —— 这就是 P0 的全部目的。"""
    service = wired
    ctx = _ctx(service, session_id="s1")
    system = _system_of(service, {"message": "帮我看看广告"}, ctx)
    assert "Hector" in system                    # USER.md（核心记忆，每轮常驻）
    assert "领星广告方法论" in system            # 分类记忆索引层
    assert "记忆摘要" in system


def test_chat_route_still_gets_memory(wired):
    """闲聊路由**不能**跳过记忆。

    知识检索在 is_chat 时跳过是对的（闲聊不需要引证），照抄那个排除条件的话，
    "我是谁""我上次说的偏好"这类最能体现记忆价值的问题反而没有记忆可用。
    """
    service = wired
    system = _system_of(service, {"message": "在吗"}, _ctx(service, session_id="s2"), _chat_route())
    assert "Hector" in system


@pytest.mark.parametrize("payload_extra, why", [
    ({"inject_retrieval": False}, "自动化轮次（任务台/巡检）显式关掉了上下文注入"),
    ({"task_id": "task-1"}, "任务轮次：机器的例行输出不该被用户画像带偏"),
    ({"no_memory": True}, "临时会话：用户明确说了这次别用记忆库"),
])
def test_injection_opt_out(wired, payload_extra, why):
    service = wired
    payload = {"message": "帮我看看广告", **payload_extra}
    system = _system_of(service, payload, _ctx(service, session_id="s3"))
    assert "Hector" not in system, why
    assert "领星广告方法论" not in system, why


def test_injection_can_be_switched_off_globally(wired):
    """默认值纪律：关掉开关必须完全回到"没有这个特性"的样子。"""
    from ivyea_agent import config
    service = wired
    config.set_setting("memory_serve_inject", False)
    system = _system_of(service, {"message": "x"}, _ctx(service, session_id="s4"))
    assert "Hector" not in system


def test_write_gate_excludes_task_turns(wired):
    service = wired
    assert service._memory_write_on({}, _ctx(service)) is True
    assert service._memory_write_on({"task_id": "t1"}, _ctx(service)) is False
    assert service._memory_write_on({}, _ctx(service, task_id="t1")) is False
    assert service._memory_write_on({"no_memory": True}, _ctx(service)) is False
    # 关掉检索注入的轮次（ivyea code）仍然是真实发生过的对话，该记
    assert service._memory_write_on({"inject_retrieval": False}, _ctx(service)) is True


def test_record_turn_writes_episodes(wired):
    from ivyea_agent import memory
    service = wired
    before = len(memory.episodes_since(0.0))
    service._record_turn_memory({}, _ctx(service, session_id="s5"), "帮我否个词", "已经否了。")
    assert len(memory.episodes_since(0.0)) == before + 2


def test_record_turn_skips_empty_answer(wired):
    """半截/空回答不是"发生过的事实" —— 记进去只会污染以后的召回和反思。"""
    from ivyea_agent import memory
    service = wired
    before = len(memory.episodes_since(0.0))
    service._record_turn_memory({}, _ctx(service, session_id="s6"), "问题", "   ")
    assert len(memory.episodes_since(0.0)) == before


def test_record_turn_respects_no_memory(wired):
    from ivyea_agent import memory
    service = wired
    before = len(memory.episodes_since(0.0))
    service._record_turn_memory({"no_memory": True}, _ctx(service, session_id="s7"), "问", "答")
    assert len(memory.episodes_since(0.0)) == before


def test_scope_defaults_off(wired):
    """作用域默认关：开了会表现为"以前想得起来的现在想不起来"，先观察一版。"""
    service = wired
    assert service._memory_scope(_ctx(service, workspace="/root/ivyea-ops")) == ""


def test_scope_from_workspace_when_enabled(wired):
    from ivyea_agent import config
    service = wired
    config.set_setting("memory_scope_from_workspace", True)
    assert service._memory_scope(_ctx(service, workspace="/root/ivyea-ops")) == "ivyea-ops"
    assert service._memory_scope(_ctx(service, workspace="")) == ""
