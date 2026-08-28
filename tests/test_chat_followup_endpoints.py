"""serve 这一层：追加指令端点、选项卡回答端点、活轮清单、逐轮时刻表。

这里跑的是**真实的 chat_stream**（桩 provider），检查它实际发出的事件和实际写进
存档的东西 —— 消费方（IvyeaOps 工作台）就是照这些字段渲染的。
"""
from __future__ import annotations

import time

from ivyea_agent import ask, live_turn, sessions, service, turn_inbox


def teardown_function():
    turn_inbox.reset_for_tests()
    ask.reset_for_tests()
    live_turn.reset_for_tests()


class _EchoProvider:
    def stream_chat(self, messages, tools=None):
        yield {"type": "text", "text": "好的。"}
        yield {"type": "final", "content": "好的。", "tool_calls": [], "usage": {}}


def _run(message: str, **payload):
    events: list[tuple[str, dict]] = []
    body = {"message": message, "persist": False, "max_steps": 2, **payload}
    result = service.chat_stream(body, lambda e, d: events.append((e, d)),
                                 provider=_EchoProvider())
    return result, events


# ── 追加指令端点 ────────────────────────────────────────────────────────────

def test_inject_is_refused_when_nothing_is_running():
    """没有活轮就明确说不收 —— 调用方据此把这句话当成下一轮发出去。

    收下才是坏的：那句话要么被下一轮莫名其妙读到，要么烂在收件箱里。
    """
    out = service.chat_inject({"session_id": "nope", "text": "补一句"})
    assert out == {"ok": True, "accepted": False, "reason": "no_live_turn", "session_id": "nope"}
    assert turn_inbox.pending("nope") == []


def test_inject_accepted_while_a_turn_is_running():
    live_turn.begin("s-live")
    try:
        out = service.chat_inject({"session_id": "s-live", "text": "顺便把标题也改了"})
        assert out["ok"] is True and out["accepted"] is True
        assert [i["text"] for i in turn_inbox.pending("s-live")] == ["顺便把标题也改了"]
    finally:
        live_turn.get("s-live").end()


def test_inject_validates_arguments():
    assert service.chat_inject({"session_id": "", "text": "x"})["ok"] is False
    assert service.chat_inject({"session_id": "s", "text": "  "})["ok"] is False


def test_a_new_turn_does_not_inherit_the_previous_inbox():
    """上一轮没被读到的话是对**那一轮**说的，调用方已另做安排，不许漏进下一轮。"""
    live_turn.begin("s-live")
    turn_inbox.submit("s-live", "上一轮的话")
    live_turn.begin("s-live")
    assert turn_inbox.pending("s-live") == []


# ── 选项卡回答端点 ──────────────────────────────────────────────────────────

def test_question_endpoint_reports_expired_requests_honestly():
    out = service.chat_question({"request_id": "gone", "answers": {"q": "a"}})
    assert out["ok"] is False and out["error"] == "unknown_or_expired_request"


def test_question_endpoint_validates_arguments():
    assert service.chat_question({"request_id": "", "answers": {"q": "a"}})["ok"] is False
    assert service.chat_question({"request_id": "x", "answers": {}})["ok"] is False


def _captured_contexts(runs: list[dict]) -> list:
    captured: list = []
    real = service._chat_messages

    def _spy(message, payload, ctx, route=None):
        captured.append(ctx)
        return real(message, payload, ctx, route)

    service._chat_messages = _spy
    try:
        for payload in runs:
            _run("你好", **payload)
    finally:
        service._chat_messages = real
    return captured


def test_ask_channel_is_wired_regardless_of_approval_tier():
    """问问题不是写操作：只读档下也得能弹选项卡，否则模型只能自己猜一条路。"""
    from ivyea_agent import agent_tools

    captured = _captured_contexts([
        {"interactive": True},                                  # 默认 = 只读档
        {"interactive": True, "approval": "auto", "plan_mode": False},
    ])
    assert all(isinstance(c, agent_tools.ToolContext) for c in captured)
    assert all(callable(c.ask_fn) for c in captured)


def test_ask_channel_is_off_unless_the_caller_says_someone_is_watching():
    """服务端自己读流的那几处（技能执行、知识库问答）没有人能点选项卡。

    默认给通道的话，模型在那种轮次里问一句，整轮就白白挂满超时时长才继续 ——
    所以这是 opt-in（同 stream_reasoning / defer_citation_text 的路数）。
    """
    captured = _captured_contexts([{}, {"interactive": False}, {"interactive": "yes"}])
    assert [c.ask_fn for c in captured] == [None, None, None]


# ── 活轮清单（左栏的闪烁标记）────────────────────────────────────────────────

def test_live_sessions_lists_only_running_turns():
    live = live_turn.begin("s-a")
    live_turn.begin("s-b").end()
    try:
        rows = service.chat_live_sessions()["sessions"]
        assert [r["id"] for r in rows] == ["s-a"]
        assert rows[0]["started_ms"] > 0
    finally:
        live.end()


def test_session_row_carries_the_running_flag():
    live = live_turn.begin("s-a")
    try:
        assert service._public_session({"id": "s-a"})["running"] is True
        assert service._public_session({"id": "s-b"})["running"] is False
    finally:
        live.end()


# ── 逐轮时刻表 ──────────────────────────────────────────────────────────────

def test_final_carries_the_turn_clock():
    started = time.time()
    result, events = _run("你好")
    final = [d for e, d in events if e == "final"][0]
    assert final["started_ms"] >= int(started * 1000) - 1000
    assert final["ended_ms"] >= final["started_ms"]
    assert final["ms"] >= 0
    assert result["ms"] == final["ms"]


def test_turn_times_are_persisted_and_projected_into_the_detail(ivyea_home):
    import importlib
    importlib.reload(sessions)
    importlib.reload(service)
    sid = sessions.new_id()
    _run("你好", session_id=sid, persist=True)

    times = sessions.turn_times(sid)
    assert len(times) == 1
    assert times[0]["turn"] == 0
    assert times[0]["started_at"] > 0 and times[0]["ended_at"] >= times[0]["started_at"]

    detail = service.chat_session_detail(sid)["session"]
    assert detail["turn_times"] == times          # 详情页拿到的就是落盘那份


def test_detail_only_returns_the_times_of_the_page_it_serves(ivyea_home):
    import importlib
    importlib.reload(sessions)
    importlib.reload(service)
    sid = sessions.new_id()
    for n in range(3):
        _run(f"第 {n} 问", session_id=sid, persist=True)

    page = service.chat_session_detail(sid, turns=1)["session"]
    assert page["turns"]["from"] == 2
    assert [t["turn"] for t in page["turn_times"]] == [2]


# ── 真·停止 ─────────────────────────────────────────────────────────────────

class _EndlessProvider:
    """永远不肯收工的模型：每一步都再调一个工具。

    没有中止的话，这样一轮会一直烧到步数上限 —— 那正是"点了停止却还在跑"时
    用户在替它付的账。
    """

    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        yield {"type": "text", "text": f"第 {self.calls} 步…"}
        yield {"type": "final", "content": "", "usage": {},
               "tool_calls": [{"id": f"c{self.calls}", "name": "list_dir",
                               "arguments": {"path": "."}}]}


def test_cancel_really_stops_the_turn(ivyea_home):
    """点停止 = 模型不会再往下走一步，而不是"我不看了、你接着烧"。"""
    import importlib
    import threading
    importlib.reload(sessions)
    importlib.reload(service)
    sid = sessions.new_id()
    provider = _EndlessProvider()
    events: list[tuple[str, dict]] = []

    def watcher():
        for _ in range(400):                       # 最多等 4 秒
            if provider.calls >= 2:
                assert service.chat_cancel({"session_id": sid})["cancelled"] is True
                return
            time.sleep(0.01)

    t = threading.Thread(target=watcher)
    t.start()
    out = service.chat_stream(
        {"message": "跑个长任务", "session_id": sid, "max_steps": 50, "persist": True},
        lambda e, d: events.append((e, d)), provider=provider)
    t.join()

    assert out["cancelled"] is True
    assert provider.calls < 50                     # 真的停在半路，不是跑满了才结束
    # 停止是**正常结局**，不能画成红色的失败
    assert [e for e, _ in events if e == "cancelled"]
    assert not [e for e, _ in events if e == "error"]
    # 已经跑出来的东西照常落盘 —— 半截也是真发生过的
    assert sessions.load(sid)["messages"]
    assert sessions.turn_times(sid)[0]["ended_at"] > 0


def test_cancel_reports_honestly_when_nothing_is_running():
    out = service.chat_cancel({"session_id": "nobody-home"})
    assert out == {"ok": True, "cancelled": False, "session_id": "nobody-home",
                   "reason": "no_live_turn"}


def test_cancel_validates_arguments():
    assert service.chat_cancel({"session_id": ""})["ok"] is False


class _SlowProvider:
    """一个字一个字慢慢吐的模型 —— 用来把"用户在模型说话时按了停止"这个时序做实。"""

    def __init__(self, on_first_token=None):
        self.calls = 0
        self._on_first_token = on_first_token

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        for i in range(200):
            if i == 0 and self._on_first_token:
                self._on_first_token()
            yield {"type": "text", "text": "字"}
            time.sleep(0.01)
        yield {"type": "final", "content": "字" * 200, "tool_calls": [], "usage": {}}


def test_cancel_hands_back_the_queued_followups(ivyea_home):
    """停在半路时，还没被读到的追加指令不能无声吞掉 —— 端回给调用方。"""
    import importlib
    importlib.reload(sessions)
    importlib.reload(service)
    sid = sessions.new_id()

    def on_first_token():
        # 模型正在吐字：这时排一条追加指令（它要到下一个步边界才会被读走），
        # 紧接着按停止 —— 于是它注定没被读到，必须被端回来。
        turn_inbox.submit(sid, "还有件事")
        service.chat_cancel({"session_id": sid})

    out = service.chat_stream({"message": "跑个长任务", "session_id": sid, "max_steps": 50},
                              lambda _e, _d: None, provider=_SlowProvider(on_first_token))
    assert out["cancelled"] is True
    assert [i["text"] for i in out.get("injected_pending") or []] == ["还有件事"]
    assert turn_inbox.pending(sid) == []


def test_cancel_stops_mid_generation(ivyea_home):
    """在模型吐字的过程中按停止，也要当场停 —— 不能等它把这一段说完。"""
    import importlib
    importlib.reload(sessions)
    importlib.reload(service)
    sid = sessions.new_id()
    started = time.time()
    out = service.chat_stream(
        {"message": "说点什么", "session_id": sid, "max_steps": 5},
        lambda _e, _d: None,
        provider=_SlowProvider(lambda: service.chat_cancel({"session_id": sid})))
    assert out["cancelled"] is True
    # 200 个 token × 10ms = 2s；当场停的话远不到
    assert time.time() - started < 1.5


def test_status_says_it_is_stopping():
    live = live_turn.begin("s-x")
    try:
        assert live_turn.status("s-x")["cancelling"] is False
        live_turn.request_cancel("s-x")
        assert live_turn.status("s-x")["cancelling"] is True
    finally:
        live.end()
