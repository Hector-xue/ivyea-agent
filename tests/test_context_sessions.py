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
    assert context.DEFAULT_AUTO_COMPACT is True           # 默认主动压缩（对标 Claude）
    config.set_setting("auto_compact", False)
    assert context.should_compact(120000) is False        # 关时不自动压
    assert context.should_warn_compact(120000) is True    # 但仍会提示手动 /compact
    config.set_setting("auto_compact", True)
    assert context.should_compact(120000) is True         # 开时越过阈值即压
    assert context.should_compact(100) is False           # 未过阈值不压


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
    new, summary = context.compact(messages, prov, keep_recent=0)   # 0=全量摘要（旧行为）
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


def _tool_call_msg(cid: str) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}


def test_compact_keeps_recent_verbatim_and_pair_safe(ivyea_home):
    """保留最近 N 条原文；naive 切点落在 tool 上时回退到其 assistant(tool_calls)。"""
    from ivyea_agent import context
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "旧1"},
        {"role": "assistant", "content": "旧2"},
        {"role": "user", "content": "旧3"},
        {"role": "assistant", "content": "旧4"},
        _tool_call_msg("t1"),                                   # ← 回退应停在这
        {"role": "tool", "tool_call_id": "t1", "content": "结果A"},
        {"role": "tool", "tool_call_id": "t1", "content": "结果B"},
        {"role": "assistant", "content": "近1"},
        {"role": "user", "content": "近2"},
    ]
    prov = _FakeProvider()
    # history 共 9 条，keep_recent=3 的 naive 切点是第二个 tool → 必须回退到 tool_calls
    new, summary = context.compact(messages, prov, keep_recent=3)
    assert summary
    # 保留段以 assistant(tool_calls) 开头，配对完整
    kept = new[3:]                       # [system, 摘要user, 确认assistant] 之后
    assert kept[0].get("tool_calls")
    assert [m.get("role") for m in kept] == ["assistant", "tool", "tool", "assistant", "user"]
    assert kept == messages[5:]          # 逐字保留原文
    # 摘要段（旧1..旧4）不含 tool 残留：每条 tool 前都有它的 assistant.tool_calls
    for i, m in enumerate(new):
        if m.get("role") == "tool":
            prev = new[i - 1]
            assert prev.get("tool_calls") or prev.get("role") == "tool"


def test_compact_keep_recent_from_config(ivyea_home):
    """默认参数读 compact_keep_recent 配置键。"""
    from ivyea_agent import config, context
    config.set_setting("compact_keep_recent", 2)
    messages = [{"role": "system", "content": "s"}] + \
        [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(8)]
    new, summary = context.compact(messages, _FakeProvider())
    assert summary
    assert new[-2:] == messages[-2:]     # 保留最近 2 条原文


def test_compact_short_history_falls_back_to_full_summary(ivyea_home):
    """扣掉保留段后可压部分不足 4 条、但历史本身够长（防溢出场景）→ 退回全量摘要。"""
    from ivyea_agent import context
    messages = [{"role": "system", "content": "s"}] + \
        [{"role": "user", "content": f"m{i}"} for i in range(5)]
    new, summary = context.compact(messages, _FakeProvider(), keep_recent=3)
    assert summary
    assert len(new) == 3                 # system + 摘要 + 确认（无保留段）


def test_compact_provider_failure_returns_original(ivyea_home):
    from ivyea_agent import context

    class _Boom:
        def complete(self, *a, **k):
            raise RuntimeError("llm down")

    messages = [{"role": "system", "content": "s"}] + \
        [{"role": "user", "content": f"m{i}"} for i in range(10)]
    new, summary = context.compact(messages, _Boom(), keep_recent=2)
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
