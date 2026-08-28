"""计划回注：每轮注回上下文、压缩后不丢、批准闸拦写。

这是本次改造的核心断言 —— 计划从「模型自己记得」改成「运行时每轮喂给它」。
"""
from __future__ import annotations

from ivyea_agent import agent_loop, config, context, plan_store
from ivyea_agent.agent_tools import ToolContext


class FakeProvider:
    def complete(self, system, user, **kw):
        return "摘要：之前在改代码"


def _plan(session_id="inj-1"):
    plan_store.sync_todos(session_id, [
        {"content": "读现状", "status": "completed"},
        {"content": "改代码", "status": "in_progress"},
        {"content": "跑测试", "status": "pending"},
    ])


def test_plan_note_is_appended_to_the_last_user_message(ivyea_home):
    _plan()
    ctx = ToolContext(workspace=".", session_id="inj-1")
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "接着做"}]
    agent_loop._inject_plan_note(ctx, messages)
    assert plan_store.PLAN_NOTE_MARKER in messages[-1]["content"]
    assert "▶ 2. 改代码" in messages[-1]["content"]
    assert messages[0]["content"] == "sys"     # system 不动，多 system 在 Anthropic 那条路会被切走


def test_plan_note_is_not_injected_twice(ivyea_home):
    _plan()
    ctx = ToolContext(workspace=".", session_id="inj-1")
    messages = [{"role": "user", "content": "接着做"}]
    agent_loop._inject_plan_note(ctx, messages)
    once = messages[-1]["content"]
    agent_loop._inject_plan_note(ctx, messages)
    assert messages[-1]["content"] == once


def test_multimodal_user_message_gets_a_text_block(ivyea_home):
    _plan()
    ctx = ToolContext(workspace=".", session_id="inj-1")
    messages = [{"role": "user", "content": [{"type": "text", "text": "看这张图"}]}]
    agent_loop._inject_plan_note(ctx, messages)
    blocks = messages[-1]["content"]
    assert isinstance(blocks, list) and len(blocks) == 2
    assert plan_store.PLAN_NOTE_MARKER in blocks[-1]["text"]


def test_no_plan_means_no_injection(ivyea_home):
    ctx = ToolContext(workspace=".", session_id="inj-empty")
    messages = [{"role": "user", "content": "你好"}]
    agent_loop._inject_plan_note(ctx, messages)
    assert messages[-1]["content"] == "你好"


def test_subagent_without_session_id_is_untouched(ivyea_home):
    """只读子 agent 没有 session_id —— 行为必须与加这套机制之前逐字相同。"""
    ctx = ToolContext(workspace=".")
    messages = [{"role": "user", "content": "查清楚 X"}]
    agent_loop._inject_plan_note(ctx, messages)
    assert messages[-1]["content"] == "查清楚 X"


def test_plan_survives_midturn_compaction(ivyea_home, monkeypatch):
    """压缩会把"干到第几步"摘要掉。计划必须原样穿过压缩，而不是靠摘要复述。"""
    _plan("inj-compact")
    monkeypatch.setattr(config, "get_setting",
                        lambda k, d=None: {"compact_hard_ceiling_tokens": 10}.get(k, d))
    ctx = ToolContext(workspace=".", session_id="inj-compact")
    messages = [
        # 每条 2200 字符（合计 ≈2300 tok）：自动压缩这条路上有 `worth_compacting` 闸，
        # 可压段不足 MIN_COMPACTIBLE_TOKENS 就不跑。别把它缩回去 —— 缩了这个用例测的
        # 就不是压缩，而是那道闸。
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u" * 2200},
        {"role": "assistant", "content": "a" * 2200},
        {"role": "user", "content": "b" * 2200},
        {"role": "assistant", "content": "c" * 2200},
    ]
    agent_loop._maybe_compact(messages, FakeProvider(), step_idx=1, narrate=lambda _s: None,
                              ctx=ctx)
    joined = "\n".join(str(m.get("content") or "") for m in messages)
    assert "摘要：之前在改代码" in joined       # 摘要照常
    assert plan_store.PLAN_NOTE_MARKER in joined  # 计划也在
    assert "▶ 2. 改代码" in joined


def test_compact_without_ctx_behaves_exactly_as_before(ivyea_home, monkeypatch):
    monkeypatch.setattr(config, "get_setting",
                        lambda k, d=None: {"compact_hard_ceiling_tokens": 10}.get(k, d))
    messages = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 60} for i in range(4)]
    agent_loop._maybe_compact(messages, FakeProvider(), step_idx=1, narrate=lambda _s: None)
    joined = "\n".join(str(m.get("content") or "") for m in messages)
    assert plan_store.PLAN_NOTE_MARKER not in joined


def test_compact_extra_note_is_verbatim(ivyea_home):
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 60}
                for i in range(6)]
    new, summary = context.compact(messages, FakeProvider(), keep_recent=0,
                                   extra_note="[当前计划] 原样保留的一段")
    assert summary
    assert "[当前计划] 原样保留的一段" in new[0]["content"]


# ── 批准闸 ───────────────────────────────────────────────────────────────────
def test_unapproved_plan_blocks_writes_outside_plan_mode(ivyea_home):
    plan_store.sync_todos("gate-1", [{"content": "改配置", "status": "pending"}], plan_mode=True)
    ctx = ToolContext(workspace=".", session_id="gate-1", plan_mode=False)
    res, _ms, blocked = agent_loop._run_one(
        {"id": "w1", "name": "write_file", "arguments": {"path": "a.py", "content": "x"}}, ctx)
    assert blocked is True and res.ok is False
    assert "还没有得到用户批准" in res.text


def test_approved_plan_lets_writes_through(ivyea_home):
    plan_store.sync_todos("gate-2", [{"content": "改配置", "status": "pending"}], plan_mode=True)
    plan_store.approve("gate-2")
    ctx = ToolContext(workspace=".", session_id="gate-2", plan_mode=False)
    assert agent_loop._guard_tool_call(ctx, {"name": "write_file", "arguments": {}}) is None


def test_ordinary_chat_is_never_gated_on_approval(ivyea_home):
    """没走过计划模式的普通对话不该凭空多出一道批准闸（老行为必须逐字保留）。"""
    plan_store.sync_todos("gate-3", [{"content": "改配置", "status": "in_progress"}])
    ctx = ToolContext(workspace=".", session_id="gate-3", plan_mode=False)
    assert agent_loop._guard_tool_call(ctx, {"name": "write_file", "arguments": {}}) is None


def test_read_tools_are_never_gated_on_approval(ivyea_home):
    plan_store.sync_todos("gate-4", [{"content": "改配置", "status": "pending"}], plan_mode=True)
    ctx = ToolContext(workspace=".", session_id="gate-4", plan_mode=False)
    assert agent_loop._guard_tool_call(ctx, {"name": "read_file", "arguments": {"path": "a"}}) is None
