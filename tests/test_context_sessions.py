"""上下文压缩 + 会话持久化。"""
from __future__ import annotations


class _FakeProvider:
    def __init__(self, summary="要点：ASIN B0X 否词3个；用户偏好保守。"):
        self.summary = summary
        self.seen = None
    def complete(self, system, user, **k):
        self.seen = (system, user)
        return self.summary


def test_should_compact_threshold(ivyea_home):
    from ivyea_agent import config, context
    assert context.should_compact(120000) is False
    assert context.should_warn_compact(120000) is True
    config.set_setting("auto_compact", True)
    assert context.should_compact(120000) is True
    assert context.should_compact(100) is False


def test_compact_replaces_history_no_tool_pairs(ivyea_home):
    from ivyea_agent import context
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "看下 B0X"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "run_patrol", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "巡检完成"},
        {"role": "assistant", "content": "有3个否词候选"},
        {"role": "user", "content": "执行吧"},
    ]
    prov = _FakeProvider()
    new, summary = context.compact(messages, prov)
    assert summary
    assert new[0]["role"] == "system"
    assert "摘要" in new[1]["content"]
    # 关键：压缩后不再含 tool 消息 / tool_calls（避免 OpenAI 配对错误）
    assert not any(m.get("role") == "tool" for m in new)
    assert not any(m.get("tool_calls") for m in new)


def test_compact_too_short_noop(ivyea_home):
    from ivyea_agent import context
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    new, summary = context.compact(messages, _FakeProvider())
    assert new == messages and summary == ""


def test_sessions_save_load_latest(ivyea_home):
    from ivyea_agent import sessions
    sid = sessions.new_id()
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "看广告"}]
    sessions.save(sid, msgs, model="deepseek-chat", usage={"cost": 0.01, "turns": 1})
    loaded = sessions.load(sid)
    assert loaded["messages"] == msgs and loaded["model"] == "deepseek-chat"
    assert sessions.latest_id() == sid
    lst = sessions.listing()
    assert lst and lst[0]["id"] == sid and "看广告" in lst[0]["preview"]


def test_sessions_new_id_is_unique(ivyea_home):
    from ivyea_agent import sessions

    ids = {sessions.new_id() for _ in range(20)}
    assert len(ids) == 20


def test_sessions_load_missing(ivyea_home):
    from ivyea_agent import sessions
    assert sessions.load("nope-does-not-exist") is None
