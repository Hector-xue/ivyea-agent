"""对话入口测试 —— 不连飞书、不连 agent。"""
import json

import pytest


from ivyea_agent.feishu_relay import chat
from ivyea_agent.feishu_relay import config
from ivyea_agent.feishu_relay import gates

ME = "ou_me"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """会话/去重文件必须落到临时目录，绝不碰真实 .state。"""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(config, "SEEN_FILE", str(tmp_path / "seen.json"))
    monkeypatch.setattr(config, "allowed_sender_ids", lambda: {ME})
    monkeypatch.setattr(config, "allowed_chat_ids", set)
    monkeypatch.setattr(gates.config, "allowed_sender_ids", lambda: {ME})
    monkeypatch.setattr(gates.config, "allowed_chat_ids", set)
    monkeypatch.setattr(config, "CHAT_PREFIX", "")


def _text(s):
    return json.dumps({"text": s}, ensure_ascii=False)


def _msg(text="你好", mid="om_1", sender=ME, chat_id="oc_1"):
    return dict(chat_id=chat_id, sender_open_id=sender, message_id=mid,
                msg_type="text", content=_text(text))


# ── 内容解析 ────────────────────────────────────────────────────────────────
def test_extract_text_and_post():
    assert chat.extract_text("text", _text("hi")) == "hi"
    post = json.dumps({"content": [[{"tag": "text", "text": "a"}],
                                   [{"tag": "text", "text": "b"}]]})
    assert chat.extract_text("post", post) == "a\nb"
    assert chat.extract_text("image", "{}") == ""
    assert chat.extract_text("text", "not json") == ""


def test_mentions_are_stripped():
    """@机器人 会留下 @_user_1 占位符，不去掉会被当正文喂给模型。"""
    assert chat.strip_mentions("@_user_1 查一下库存") == "查一下库存"


# ── 白名单 / 去重 ───────────────────────────────────────────────────────────
def test_non_whitelisted_sender_gets_no_reply(monkeypatch):
    called = []
    monkeypatch.setattr(chat, "run_turn", lambda *a: called.append(1) or "x")
    assert chat.handle_message(**_msg(sender="ou_stranger")) == ""
    assert called == []


def test_replayed_message_is_ignored(monkeypatch):
    n = {"c": 0}
    monkeypatch.setattr(chat, "run_turn", lambda *a: n.__setitem__("c", n["c"] + 1) or "ok")
    assert chat.handle_message(**_msg(mid="om_x")) == "ok"
    assert chat.handle_message(**_msg(mid="om_x")) == ""
    assert n["c"] == 1, "重投又跑了一遍 agent"


def test_seen_list_is_capped(monkeypatch):
    monkeypatch.setattr(config, "SEEN_MAX", 5)
    for i in range(20):
        chat.seen_message(f"om_{i}")
    assert len(json.load(open(config.SEEN_FILE))) <= 5


# ── 命令 ────────────────────────────────────────────────────────────────────
def test_help_and_reset(monkeypatch):
    monkeypatch.setattr(chat, "run_turn", lambda *a: "不该被调用")
    assert "Ivyea Agent" in chat.handle_message(**_msg("/help"))
    chat.set_session("oc_1", "s1")
    assert "已清空" in chat.handle_message(**_msg("/reset", mid="om_2"))
    assert chat.get_session("oc_1") is None
    assert "本来就是空的" in chat.handle_message(**_msg("/reset", mid="om_3"))


def test_prefix_filter(monkeypatch):
    monkeypatch.setattr(config, "CHAT_PREFIX", "/ivyea")
    monkeypatch.setattr(chat, "run_turn", lambda cid, t: f"收到:{t}")
    assert chat.handle_message(**_msg("闲聊")) == ""
    assert chat.handle_message(**_msg("/ivyea 查库存", mid="om_9")) == "收到:查库存"


def test_non_text_message(monkeypatch):
    m = _msg()
    m.update(msg_type="image", content="{}")
    assert "只处理文本" in chat.handle_message(**m)


# ── 会话映射 ────────────────────────────────────────────────────────────────
class _R:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


def test_session_is_created_then_reused(monkeypatch):
    seen = []

    def _post(url, json=None, timeout=None):
        seen.append(json)
        return _R({"ok": True, "session_id": "sess-1", "text": "答复"})

    monkeypatch.setattr(chat.httpx, "post", _post)
    assert chat.run_turn("oc_1", "第一句") == "答复"
    assert "session_id" not in seen[0]
    assert chat.get_session("oc_1") == "sess-1"

    chat.run_turn("oc_1", "第二句")
    assert seen[1]["session_id"] == "sess-1", "第二轮没带上会话，上下文会断"


def test_read_only_by_default(monkeypatch):
    """飞书里随口一句话不该触发写操作。"""
    seen = []
    monkeypatch.setattr(chat.httpx, "post",
                        lambda url, json=None, timeout=None: seen.append(json) or
                        _R({"ok": True, "session_id": "s", "text": "x"}))
    chat.run_turn("oc_1", "把预算调高")
    assert seen[0]["plan_mode"] is True


def test_agent_down_is_reported_not_swallowed(monkeypatch):
    def _boom(*a, **k):
        raise chat.httpx.ConnectError("connection refused")

    monkeypatch.setattr(chat.httpx, "post", _boom)
    out = chat.run_turn("oc_1", "在吗")
    assert "无法访问" in out and "connection refused" in out


def test_agent_error_is_surfaced(monkeypatch):
    monkeypatch.setattr(chat.httpx, "post",
                        lambda *a, **k: _R({"ok": False, "error": "model_not_configured",
                                            "detail": "缺 key"}))
    out = chat.run_turn("oc_1", "在吗")
    assert "model_not_configured" in out and "缺 key" in out


def test_unparseable_response(monkeypatch):
    class _Bad:
        status_code = 502

        def json(self):
            raise ValueError("nope")

    monkeypatch.setattr(chat.httpx, "post", lambda *a, **k: _Bad())
    assert "不可解析" in chat.run_turn("oc_1", "x")


def test_sessions_file_survives_corruption(monkeypatch):
    with open(config.SESSIONS_FILE, "w") as fh:
        fh.write("{ broken")
    assert chat.get_session("oc_1") is None      # 损坏不该炸，退回空
    chat.set_session("oc_1", "s2")
    assert chat.get_session("oc_1") == "s2"


# ── P6：阈值 / 写开关命令 ───────────────────────────────────────────────────
def test_threshold_list(monkeypatch):
    monkeypatch.setattr(chat, "_call_action",
                        lambda p: (200, {"ok": True, "thresholds": [
                            {"key": "a.b", "default": 1, "current": 2, "overridden": True},
                            {"key": "c.d", "default": 5, "current": 5, "overridden": False}]}))
    out = chat.handle_message(**_msg("/threshold"))
    assert "a.b" in out and "✏️" in out and "默认 1" in out


def test_threshold_set(monkeypatch):
    seen = {}
    monkeypatch.setattr(chat, "_call_action",
                        lambda p: seen.update(p) or (200, {"ok": True, "key": p["key"],
                                                           "value": 1.8}))
    out = chat.handle_message(**_msg("/threshold ads.acos_breach.factor 1.8"))
    assert seen["action"] == "threshold_set" and seen["key"] == "ads.acos_breach.factor"
    assert "立即生效" in out


def test_threshold_bad_key_is_reported(monkeypatch):
    monkeypatch.setattr(chat, "_call_action",
                        lambda p: (404, {"ok": False, "error": "未知阈值：nope"}))
    assert "未知阈值" in chat.handle_message(**_msg("/threshold nope 1"))


def test_threshold_reset(monkeypatch):
    monkeypatch.setattr(chat, "_call_action", lambda p: (200, {"ok": True, "reset": 3}))
    assert "已复位 3 项" in chat.handle_message(**_msg("/threshold reset"))


def test_operate_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(chat, "_call_action",
                        lambda p: seen.update(p) or (200, {"ok": True,
                                                           "detail": "已开启 30 分钟"}))
    out = chat.handle_message(**_msg("/operate 30"))
    assert seen["action"] == "operate_on" and seen["minutes"] == 30
    assert "已开启" in out


def test_operate_defaults_to_120(monkeypatch):
    seen = {}
    monkeypatch.setattr(chat, "_call_action",
                        lambda p: seen.update(p) or (200, {"ok": True, "detail": "ok"}))
    chat.handle_message(**_msg("/operate"))
    assert seen["minutes"] == 120
