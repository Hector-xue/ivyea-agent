"""一轮**开跑就落盘**，而不是跑完才落盘。

真实反馈（连着两三次）："做一半切到别的对话，回来之后过去几个小时的对话记录都没了，
回到同一个时间点的对话记录，之后的全部都找不到。"

根因就是这里：整轮只在收尾时写一次盘。于是
  · 跑着的会话在工作台左栏根本不存在（左栏列的是和 agent 实存对得上的会话）；
  · 中途切走再回来，前端内存里那份没了、磁盘上又没有，整段对话凭空消失；
  · 断链/中止的轮次一个字都不留 —— 用户看到的"同一个时间点"就是最后一轮**跑完**的地方。

所以这里钉两件事：开跑就有盘上记录；收尾时不把用户那句话写第二遍。
"""
from __future__ import annotations

import importlib
import sys


def _mods():
    sessions = importlib.reload(sys.modules["ivyea_agent.sessions"]) \
        if "ivyea_agent.sessions" in sys.modules else importlib.import_module("ivyea_agent.sessions")
    service = importlib.import_module("ivyea_agent.service")
    service.sessions = sessions
    return sessions, service


class _FakeProvider:
    """一轮跑完就吐一句回答的假 provider —— 这里验的是落盘时机，不是模型。"""

    name = "fake"

    def stream_chat(self, messages, tools=None):
        yield {"type": "text", "text": "好的。"}
        yield {"type": "final", "content": "好的。", "tool_calls": [], "usage": {}}

    def chat(self, messages, tools=None):
        return {"content": "好的。", "tool_calls": []}


def _run(service, payload, sink):
    def send(event, data):
        sink.append((event, data))
    return service.chat_stream(payload, send, provider=_FakeProvider())


def test_user_message_is_on_disk_before_the_turn_finishes(ivyea_home, monkeypatch):
    sessions, service = _mods()
    seen: list[dict] = []

    # 在模型开跑的那一刻去看磁盘：这时候会话文件就该已经有用户那句话了。
    real_run = service.agent_loop.run_turn_stream

    def spy(*args, **kwargs):
        seen.append(sessions.load(sid) or {})
        return real_run(*args, **kwargs)

    monkeypatch.setattr(service.agent_loop, "run_turn_stream", spy)

    sid = "20260820-000000-100-test"
    _run(service, {"message": "帮我看看这个报错", "session_id": sid,
                   "persist": True, "inject_retrieval": False}, [])

    assert seen, "run_turn_stream 没被调用，用例本身失效了"
    early = seen[0]
    roles = [m.get("role") for m in (early.get("messages") or [])]
    assert "user" in roles, f"模型还没开口时，用户那句话就该在盘上了：{roles}"
    texts = [str(m.get("content") or "") for m in (early.get("messages") or [])
             if m.get("role") == "user"]
    assert any("帮我看看这个报错" in t for t in texts)


def test_the_question_is_not_written_twice(ivyea_home):
    sessions, service = _mods()
    sid = "20260820-000000-101-test"
    _run(service, {"message": "帮我看看这个报错", "session_id": sid,
                   "persist": True, "inject_retrieval": False}, [])
    msgs = (sessions.load(sid) or {}).get("messages") or []
    asked = [m for m in msgs if m.get("role") == "user" and "帮我看看这个报错" in str(m.get("content") or "")]
    assert len(asked) == 1, f"开跑写一次、收尾又写一次 = 同一句话在会话里出现两遍：{len(asked)}"


def test_turn_count_is_still_one(ivyea_home):
    """开跑那次不带 turn_stat，所以累计账里的轮数不能被算成两轮。"""
    sessions, service = _mods()
    sid = "20260820-000000-102-test"
    _run(service, {"message": "第一个问题", "session_id": sid,
                   "persist": True, "inject_retrieval": False}, [])
    assert ((sessions.load(sid) or {}).get("stats") or {}).get("turns") == 1


def test_persist_false_still_writes_nothing(ivyea_home):
    """跟进建议、内部一次性调用走 persist=False —— 它们不该在会话库里留下孤儿。"""
    sessions, service = _mods()
    sid = "20260820-000000-103-test"
    _run(service, {"message": "内部调用", "session_id": sid,
                   "persist": False, "inject_retrieval": False}, [])
    assert sessions.load(sid) is None
