"""附图内容必须并进 **user 消息**，不能只活在 payload["system"] 里。

真实投诉：任务台里贴了一张图问"这张图里面是什么"，模型答得完全对；下一轮问它
"你是通过什么识别图片的"，它却说自己没收到过图、上一轮的描述是编的 —— 会话存档
里也确实找不到那张图。

根因：ops 把视觉模型读出的文字塞在 payload["system"]，而 system 每轮重建、落盘时
被本轮这份整个覆盖（sessions.append_turn），于是"图里是什么"只在贴图那一轮存在。
这里钉住新契约：payload["attachments"] 的内容进 user 消息、进磁盘、进下一轮历史。
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
    name = "fake"

    def stream_chat(self, messages, tools=None):
        yield {"type": "text", "text": "好的。"}
        yield {"type": "final", "content": "好的。", "tool_calls": [], "usage": {}}

    def chat(self, messages, tools=None):
        return {"content": "好的。", "tool_calls": []}


def _run(service, payload):
    return service.chat_stream(payload, lambda event, data: None, provider=_FakeProvider())


_ATTACH = [{"kind": "image", "name": "trail-cam.png", "ref": "ivyea-ref://abc123",
            "by": "siliconflow · Qwen3-VL",
            "text": "一只红熊猫趴在树干上，右侧绑着一台太阳能野外相机。"}]


def test_note_carries_content_ref_and_the_no_denial_rule():
    service = importlib.import_module("ivyea_agent.service")
    note = service._attachments_note({"attachments": _ATTACH})
    assert note.startswith(service.ATTACHMENT_MARKER), "展示端按这个标记截断，前缀不能变"
    assert "红熊猫" in note and "ivyea-ref://abc123" in note and "trail-cam.png" in note
    # 用户问的正是"你是通过什么识别图片的"：代读模型的名字必须能答得出来
    assert "Qwen3-VL" in note
    # 模型否认收到过图正是这次的投诉本身，指令必须在场
    assert "不要否认收到过图" in note


def test_no_attachments_changes_nothing():
    service = importlib.import_module("ivyea_agent.service")
    assert service._attachments_note({}) == ""
    assert service._attachments_note({"attachments": []}) == ""
    # 读不出内容的图不摆空壳
    assert service._attachments_note({"attachments": [{"ref": "ivyea-ref://x", "text": "  "}]}) == ""


def test_note_is_persisted_on_the_user_message(ivyea_home):
    sessions, service = _mods()
    sid = "20260821-000000-200-test"
    _run(service, {"message": "这张图里面是什么？", "session_id": sid, "persist": True,
                   "inject_retrieval": False, "attachments": _ATTACH})
    msgs = (sessions.load(sid) or {}).get("messages") or []
    asked = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
    assert asked and "这张图里面是什么？" in asked[0]
    assert "红熊猫" in asked[0], "附图内容没落进 user 消息 —— 下一轮它就等于没发生过"


def test_next_turn_still_sees_the_image(ivyea_home):
    """第二轮不带任何附图，模型手里也必须还有第一轮那张图读出来的内容。"""
    sessions, service = _mods()
    sid = "20260821-000000-201-test"
    _run(service, {"message": "这张图里面是什么？", "session_id": sid, "persist": True,
                   "inject_retrieval": False, "attachments": _ATTACH})

    seen: list[list[dict]] = []
    real = service.agent_loop.run_turn_stream

    def spy(provider, ctx, messages, *args, **kwargs):
        seen.append([dict(m) for m in messages])
        return real(provider, ctx, messages, *args, **kwargs)

    service.agent_loop.run_turn_stream = spy
    try:
        _run(service, {"message": "你是通过什么识别图片的", "session_id": sid,
                       "persist": True, "inject_retrieval": False})
    finally:
        service.agent_loop.run_turn_stream = real

    assert seen, "run_turn_stream 没被调用，用例本身失效了"
    body = "\n".join(str(m.get("content") or "") for m in seen[0] if m.get("role") == "user")
    assert "红熊猫" in body, "第二轮历史里没有图的内容 —— 模型只能否认自己看过图"
