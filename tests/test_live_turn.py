"""活轮日志：正在跑的那一轮，别人也能看到。

回归的是三条真实反馈（同一个根因）：
  · 切走再切回来，执行过程整块消失，只剩自己发的那句话；
  · 刷新看不到进度，要等整轮跑完再刷新一次才"一下子全出来"；
  · 模型在收尾阶段报错时，界面上明明有完整正文，刷新之后什么都没有 —— 会话
    文件里也确实没落。

前两条靠 live_turn（跑的过程中就能被别人读到），第三条靠"任何退出路径都落盘"。
"""
from __future__ import annotations

import threading


def _drain(live, from_seq=0, stop_after_end=True):
    """把 follow() 的输出收成列表。轮次已结束时它会自己收摊。"""
    out = []
    for event, data in live.follow(from_seq, alive=lambda: True, poll=0.01):
        out.append((event, data))
        if stop_after_end and event == "live_end":
            break
    return out


def test_replay_keeps_text_and_steps_interleaved():
    """正文和工具的**交错顺序**必须逐字还原 —— 分段汇报全靠它。"""
    from ivyea_agent import live_turn

    live_turn.reset_for_tests()
    live = live_turn.begin("s1")
    live.record("start", {"session_id": "s1"})
    live.record("token", {"text": "先看一眼配置。"})
    live.record("step", {"id": "c1", "name": "read_file", "status": "running"})
    live.record("step", {"id": "c1", "name": "read_file", "status": "ok"})
    live.record("token", {"text": "确认没问题，开始部署。"})
    live.record("step", {"id": "c2", "name": "run_command", "status": "running"})
    live.end()

    seen = _drain(live)
    kinds = [e for e, _ in seen]
    assert kinds[0] == "live_begin" and kinds[-1] == "live_end"
    body = kinds[1:-1]
    # start → 第一段话 → 两条 step → 第二段话 → 第三条 step
    assert body == ["start", "token", "step", "step", "token", "step"]
    texts = [d["text"] for e, d in seen if e == "token"]
    assert texts == ["先看一眼配置。", "确认没问题，开始部署。"]


def test_answer_reset_clears_the_draft_for_a_live_follower():
    """引证门打回重写：**正在跟着看**的那一份必须把上一稿丢掉，不能两稿粘在一起。"""
    from ivyea_agent import live_turn

    live_turn.reset_for_tests()
    live = live_turn.begin("s2")
    live.record("token", {"text": "草稿一"})

    got: list = []
    done = threading.Event()

    def follower():
        for event, data in live.follow(0, alive=lambda: True, poll=0.01):
            got.append((event, data))
            if event == "live_end":
                break
        done.set()

    t = threading.Thread(target=follower, daemon=True)
    t.start()
    # 跟随者先拿到草稿一，再收到作废通知，最后拿终稿。
    for _ in range(200):
        if any(e == "token" for e, _ in got):
            break
        threading.Event().wait(0.01)
    live.record("answer_reset", {"reason": "gate:citation"})
    live.record("token", {"text": "终稿"})
    live.end()
    assert done.wait(5)
    assert [e for e, _ in got if e in ("token", "answer_reset")] == ["token", "answer_reset", "token"]
    assert [d["text"] for e, d in got if e == "token"] == ["草稿一", "终稿"]


def test_a_late_follower_never_sees_a_discarded_draft():
    """半路接进来的那一份只该看到活着的那一稿 —— 作废的草稿不该再冒出来。"""
    from ivyea_agent import live_turn

    live_turn.reset_for_tests()
    live = live_turn.begin("s2b")
    live.record("token", {"text": "草稿一"})
    live.record("answer_reset", {"reason": "gate:citation"})
    live.record("token", {"text": "终稿"})
    live.end()

    seen = _drain(live)
    assert "".join(d["text"] for e, d in seen if e == "token") == "终稿"


def test_a_follower_joining_late_gets_everything_so_far():
    """半路接进来（切回页面）：从第 0 条开始把整轮补上，不是只看到之后的。"""
    from ivyea_agent import live_turn

    live_turn.reset_for_tests()
    live = live_turn.begin("s3")
    live.record("token", {"text": "已经跑了一半"})
    live.record("step", {"id": "c1", "name": "grep", "status": "ok"})

    got: list = []
    done = threading.Event()

    def follower():
        for event, data in live.follow(0, alive=lambda: True, poll=0.01):
            got.append((event, data))
            if event == "live_end":
                break
        done.set()

    t = threading.Thread(target=follower, daemon=True)
    t.start()
    live.record("token", {"text": "，现在收尾"})
    live.end()
    assert done.wait(5), "follower 没有在轮次结束后收摊"
    assert "".join(d["text"] for e, d in got if e == "token") == "已经跑了一半，现在收尾"
    assert any(e == "step" for e, _ in got)


def test_status_reports_a_running_turn():
    from ivyea_agent import live_turn

    live_turn.reset_for_tests()
    assert live_turn.status("s4") == {"running": False, "seq": 0}
    live = live_turn.begin("s4")
    live.record("start", {})
    assert live_turn.status("s4")["running"] is True
    live.end()
    assert live_turn.status("s4")["running"] is False


# ── 跑出来的东西一定落盘 ────────────────────────────────────────────────────

class _DyingProvider:
    """先吐一整段正文，再在收尾那一下报错 —— 额度用尽/断流的真实形状。"""

    def stream_chat(self, messages, tools=None):
        from ivyea_agent.providers import LLMError

        yield {"type": "text", "text": "已经写完的那半篇报告"}
        raise LLMError("余额不足")

    def chat(self, messages, tools=None):
        from ivyea_agent.providers import LLMError

        raise LLMError("余额不足")


def test_a_model_error_at_the_end_still_persists_what_was_streamed(ivyea_home):
    """界面上有字、刷新之后没了 —— 因为报错那条路径压根不落盘。这里钉死它。"""
    import importlib

    from ivyea_agent import live_turn, sessions
    importlib.reload(sessions)
    live_turn.reset_for_tests()
    from ivyea_agent import service

    events: list[tuple[str, dict]] = []
    body = {"message": "写个报告", "persist": True, "max_steps": 2}
    result = service.chat_stream(body, lambda e, d: events.append((e, d)),
                                 provider=_DyingProvider())
    assert result.get("error") == "model_error"
    sid = next(d["session_id"] for e, d in events if e == "start")

    saved = sessions.load(sid) or {}
    answers = [m for m in (saved.get("messages") or [])
               if m.get("role") == "assistant" and str(m.get("content") or "").strip()]
    assert answers, "模型报错那一轮把已经流出去的正文丢了"
    assert "已经写完的那半篇报告" in answers[-1]["content"]


def test_the_live_journal_is_closed_when_the_turn_dies(ivyea_home):
    """轮次怎么结束都要封存日志，否则跟随者永远挂着等一个不来的 final。"""
    import importlib

    from ivyea_agent import live_turn, sessions
    importlib.reload(sessions)
    live_turn.reset_for_tests()
    from ivyea_agent import service

    events: list[tuple[str, dict]] = []
    service.chat_stream({"message": "写个报告", "persist": True, "max_steps": 2},
                        lambda e, d: events.append((e, d)), provider=_DyingProvider())
    sid = next(d["session_id"] for e, d in events if e == "start")
    assert live_turn.status(sid)["running"] is False


def test_the_live_endpoint_replays_over_http(ivyea_home):
    """真起一个 serve，用 HTTP 接进去 —— 路由分发（/live 不能被当成会话 id）
    和 SSE 的形状一起验。"""
    import json
    import urllib.request

    from ivyea_agent import live_turn, service

    live_turn.reset_for_tests()
    live = live_turn.begin("sess-http")
    live.record("start", {"session_id": "sess-http"})
    live.record("token", {"text": "在读配置"})
    live.record("step", {"id": "c1", "name": "read_file", "status": "ok"})
    live.end()

    server = service.make_server("127.0.0.1", 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/chat/sessions/sess-http/live", timeout=10) as resp:
            assert resp.headers.get("Content-Type", "").startswith("text/event-stream")
            raw = resp.read().decode("utf-8")
        # 没有活轮的会话要回 404，不能把 "live" 当成会话 id 去找存档
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/chat/sessions/nobody/live", timeout=10)
            raise AssertionError("没有活轮时应当 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()

    events = [ln[len("event: "):] for ln in raw.splitlines() if ln.startswith("event: ")]
    assert events[0] == "live_begin" and events[-1] == "live_end"
    assert "step" in events and "token" in events
    payloads = [json.loads(ln[len("data: "):]) for ln in raw.splitlines() if ln.startswith("data: ")]
    assert any(p.get("text") == "在读配置" for p in payloads)
