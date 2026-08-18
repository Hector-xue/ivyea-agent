"""审批三档 + 上下文用量快照。

这两件事都属于"界面上那个开关到底有没有接到线上"：
- 审批档位选了「完全放行」，如果 execute 没开、系统提示词还在说"当前只读"，
  用户看到的就是一个点了没反应的假开关（历史上那句只读提示确实是无条件拼的）。
- 上下文进度条如果没有 window/breakdown，画出来的百分比就是编的。

用桩 provider 把真实的 chat_stream 跑起来，检查**实际发给模型的东西**和**实际发出的事件**。
"""
from __future__ import annotations

import json


class _EchoProvider:
    def __init__(self):
        self.calls: list[tuple[list, list | None]] = []

    def stream_chat(self, messages, tools=None):
        self.calls.append(([dict(m) for m in messages], tools))
        yield {"type": "text", "text": "好的。"}
        yield {"type": "final", "content": "好的。", "tool_calls": [], "usage": {}}

    def chat(self, messages, tools=None):
        self.calls.append(([dict(m) for m in messages], tools))
        return {"content": "好的。", "tool_calls": []}


def _run(message: str, **payload):
    from ivyea_agent import service

    provider = _EchoProvider()
    events: list[tuple[str, dict]] = []
    body = {"message": message, "persist": False, "max_steps": 2, **payload}
    result = service.chat_stream(body, lambda e, d: events.append((e, d)), provider=provider)
    return result, provider, events


def _system_text(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "system":
            return str(m.get("content") or "")
    return ""


# ── 审批三档 ───────────────────────────────────────────────────────────────

def test_approval_mode_normalizes_aliases():
    from ivyea_agent import service

    assert service._approval_mode(None) == "none"
    assert service._approval_mode("remote") == "remote"
    assert service._approval_mode("ask") == "remote"
    assert service._approval_mode("auto") == "auto"
    assert service._approval_mode("approve-all") == "auto"
    # 认不出来的一律按只读，绝不"猜"成放行
    assert service._approval_mode("yolo") == "none"


def test_readonly_is_still_the_default(ivyea_home):
    """不传 approval：只读，系统提示词照旧说只读。"""
    _, provider, events = _run("帮我把预算调低")
    start = next(d for e, d in events if e == "start")
    assert start["read_only"] is True
    assert start["approval"] == "none"
    assert "当前只读" in _system_text(provider.calls[0][0])


def test_remote_approval_tells_the_model_it_may_execute(ivyea_home):
    """逐项审批：execute 开，且提示词不再让它退回「只给方案」。"""
    _, provider, events = _run("帮我把预算调低", approval="remote", plan_mode=False)
    start = next(d for e, d in events if e == "start")
    assert start["approval"] == "remote"
    assert start["read_only"] is False
    system = _system_text(provider.calls[0][0])
    assert "逐项审批" in system
    assert "当前只读" not in system


def test_auto_approval_tells_the_model_it_is_authorized(ivyea_home):
    """完全放行：提示词说清已获授权，别再逐条问人要不要执行。"""
    _, provider, events = _run("把这几个词否掉", approval="auto", plan_mode=False)
    start = next(d for e, d in events if e == "start")
    assert start["approval"] == "auto"
    assert start["read_only"] is False
    system = _system_text(provider.calls[0][0])
    assert "完全放行" in system
    assert "当前只读" not in system


def test_auto_approval_in_plan_mode_stays_readonly(ivyea_home):
    """完全放行 + 计划模式 = 仍然只读。两个开关打架时，安全的那个赢。"""
    _, provider, events = _run("把这几个词否掉", approval="auto", plan_mode=True)
    start = next(d for e, d in events if e == "start")
    assert start["read_only"] is True
    assert "当前只读" in _system_text(provider.calls[0][0])


def test_auto_approval_opens_execute_on_the_context():
    """直接盯 ToolContext：execute / accept_edits 必须真的打开，光看提示词不算数。"""
    from ivyea_agent import service

    captured: list = []
    real_messages = service._chat_messages

    def _spy(message, payload, ctx, route=None):
        captured.append(ctx)
        return real_messages(message, payload, ctx, route)

    service._chat_messages = _spy
    try:
        _run("把这几个词否掉", approval="auto", plan_mode=False)
        _run("把这几个词否掉", approval="remote", plan_mode=False)
        _run("把这几个词否掉")
    finally:
        service._chat_messages = real_messages

    auto_ctx, remote_ctx, readonly_ctx = captured
    assert (auto_ctx.execute, auto_ctx.perm.accept_edits) == (True, True)
    # 逐项审批要执行，但**不能**自动接受 —— 那样确认卡就永远不弹了
    assert (remote_ctx.execute, remote_ctx.perm.accept_edits) == (True, False)
    assert (readonly_ctx.execute, readonly_ctx.perm.accept_edits) == (False, False)


def test_chat_run_only_executes_on_auto(ivyea_home):
    """非流式入口没有确认卡通道：remote 在这里仍然只读，只有 auto 能开写。"""
    from ivyea_agent import service

    captured: list = []
    real_messages = service._chat_messages

    def _spy(message, payload, ctx, route=None):
        captured.append(ctx)
        return real_messages(message, payload, ctx, route)

    service._chat_messages = _spy
    try:
        service.chat_run({"message": "你好", "persist": False, "max_steps": 1,
                          "approval": "remote", "plan_mode": False}, provider=_EchoProvider())
        service.chat_run({"message": "你好", "persist": False, "max_steps": 1,
                          "approval": "auto", "plan_mode": False}, provider=_EchoProvider())
    finally:
        service._chat_messages = real_messages

    remote_ctx, auto_ctx = captured
    assert remote_ctx.execute is False
    assert auto_ctx.execute is True and auto_ctx.perm.accept_edits is True


# ── 上下文快照 ─────────────────────────────────────────────────────────────

def test_window_for_known_and_unknown_models(ivyea_home):
    from ivyea_agent import context

    assert context.window_for("claude-opus-4-8") == 200_000
    assert context.window_for("deepseek-v4-pro") == 128_000
    # 认不出来的模型给保守窗口，而不是编一个 1M
    assert context.window_for("某个自建模型") == context.DEFAULT_WINDOW


def test_window_override_from_config(ivyea_home):
    from ivyea_agent import config, context

    config.set_setting("context_window", 32_000)
    assert context.window_for("deepseek-v4-pro") == 32_000


def test_snapshot_splits_system_tools_and_messages(ivyea_home):
    from ivyea_agent import context

    messages = [{"role": "system", "content": "系统提示" * 200},
                {"role": "user", "content": "帮我看下广告"}]
    tools = [{"type": "function", "function": {"name": "t_read_file", "parameters": {}}}]
    snap = context.snapshot(messages, tools, "deepseek-v4-pro")

    b = snap["breakdown"]
    assert b["system"] > b["messages"] > 0
    assert b["tools"] > 0
    assert snap["used"] == b["system"] + b["tools"] + b["messages"]
    assert snap["window"] == 128_000
    assert snap["percent"] == round(snap["used"] * 100.0 / 128_000, 2)
    assert snap["estimated"] is True


def test_snapshot_counts_full_tool_table_when_tools_is_none(ivyea_home):
    """tools=None 表示"交给 agent_loop 兜底成全量"。跟着按全量算，否则最大的一块被漏掉。"""
    from ivyea_agent import context

    messages = [{"role": "user", "content": "你好"}]
    assert context.snapshot(messages, None, "x")["breakdown"]["tools"] > 1000
    assert context.snapshot(messages, [], "x")["breakdown"]["tools"] == 0


def test_chat_stream_emits_context_before_tokens_and_in_final(ivyea_home):
    """进度条要在第一个字之前就能画出来，收尾时再更新到本轮之后的位置。"""
    result, _, events = _run("新卖家注册身份验证失败怎么办")

    names = [e for e, _ in events]
    assert "context" in names
    assert names.index("context") < names.index("token"), "上下文事件必须早于第一个 token"

    ctx_event = next(d for e, d in events if e == "context")
    assert ctx_event["window"] > 0
    assert ctx_event["used"] > 0
    assert set(ctx_event["breakdown"]) == {"system", "tools", "messages"}
    # 常规路线挂全量工具，工具那一档必须是本轮最大的一块之一
    assert ctx_event["breakdown"]["tools"] > 1000

    final_ctx = result["context"]
    assert final_ctx["used"] >= ctx_event["used"], "收尾时上下文只会更长"
    json.dumps(final_ctx)      # 必须能原样进 SSE
