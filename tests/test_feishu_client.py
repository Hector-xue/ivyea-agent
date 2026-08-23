"""飞书客户端测试 —— 全部走 mock，不打真实网络。"""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture()
def wired(ivyea_home, monkeypatch):
    from ivyea_agent import config, feishu_client

    monkeypatch.setenv("IVYEA_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("IVYEA_FEISHU_APP_SECRET", "secret_test")
    s = config.load_settings()
    s["feishu_default_chat_id"] = "oc_default"
    config.save_settings(s)
    return feishu_client


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


def test_token_is_cached_and_reused(wired, monkeypatch):
    calls = {"n": 0}

    def fake_post(url, json=None, **kw):
        calls["n"] += 1
        return _Resp({"code": 0, "tenant_access_token": "t1", "expire": 7200})

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        post = staticmethod(fake_post)

    monkeypatch.setattr(wired.httpx, "Client", _C)
    assert wired.access_token() == "t1"
    assert wired.access_token() == "t1"
    assert calls["n"] == 1, "token 应命中缓存，不该每次都取"


def test_expiring_token_is_refreshed_early(wired, monkeypatch):
    """提前 5 分钟刷新——否则边界上会用到刚过期的 token。"""
    wired._save_token({"token": "old", "expires_at": time.time() + 60})

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, **kw):
            return _Resp({"code": 0, "tenant_access_token": "fresh", "expire": 7200})

    monkeypatch.setattr(wired.httpx, "Client", _C)
    assert wired.access_token() == "fresh"


def test_invalid_token_triggers_one_retry(wired, monkeypatch):
    """服务端说 token 失效时以服务端为准，强刷一次重试；不能无限递归。"""
    wired._save_token({"token": "stale", "expires_at": time.time() + 9999})
    seq = []

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, **kw):
            return _Resp({"code": 0, "tenant_access_token": "new", "expire": 7200})
        def request(self, method, url, headers=None, params=None, json=None):
            seq.append(headers["Authorization"])
            if len(seq) == 1:
                return _Resp({"code": 99991663, "msg": "token invalid"})
            return _Resp({"code": 0, "data": {"message_id": "om_1"}})

    monkeypatch.setattr(wired.httpx, "Client", _C)
    assert wired.send_card("oc_x", {"a": 1}) == "om_1"
    assert seq == ["Bearer stale", "Bearer new"]


def test_repeated_invalid_token_does_not_loop(wired, monkeypatch):
    wired._save_token({"token": "stale", "expires_at": time.time() + 9999})
    n = {"c": 0}

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, **kw):
            return _Resp({"code": 0, "tenant_access_token": "x", "expire": 7200})
        def request(self, method, url, headers=None, params=None, json=None):
            n["c"] += 1
            return _Resp({"code": 99991663, "msg": "token invalid"})

    monkeypatch.setattr(wired.httpx, "Client", _C)
    with pytest.raises(wired.FeishuError):
        wired.send_card("oc_x", {"a": 1})
    assert n["c"] == 2, "最多重试一次"


def test_send_card_serializes_content(wired, monkeypatch):
    seen = {}

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, **kw):
            return _Resp({"code": 0, "tenant_access_token": "t", "expire": 7200})
        def request(self, method, url, headers=None, params=None, json=None):
            seen.update({"method": method, "url": url, "params": params, "body": json})
            return _Resp({"code": 0, "data": {"message_id": "om_9"}})

    monkeypatch.setattr(wired.httpx, "Client", _C)
    wired.send_card("", {"header": {"x": "中文"}})
    assert seen["params"] == {"receive_id_type": "chat_id"}
    assert seen["body"]["receive_id"] == "oc_default"      # 回落到默认会话
    assert seen["body"]["msg_type"] == "interactive"
    # content 必须是 JSON 字符串，且中文不能被转义成 \uXXXX
    assert isinstance(seen["body"]["content"], str)
    assert "中文" in seen["body"]["content"]
    assert json.loads(seen["body"]["content"])["header"]["x"] == "中文"


def test_missing_chat_id_errors(wired, monkeypatch):
    from ivyea_agent import config

    s = config.load_settings()
    s["feishu_default_chat_id"] = ""
    config.save_settings(s)
    with pytest.raises(wired.FeishuError):
        wired.send_card("", {"a": 1})


def test_api_error_is_raised_with_code(wired, monkeypatch):
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, **kw):
            return _Resp({"code": 0, "tenant_access_token": "t", "expire": 7200})
        def request(self, method, url, headers=None, params=None, json=None):
            return _Resp({"code": 230001, "msg": "bot not in chat"})

    monkeypatch.setattr(wired.httpx, "Client", _C)
    with pytest.raises(wired.FeishuError) as e:
        wired.send_card("oc_x", {"a": 1})
    assert e.value.code == 230001


def test_not_configured(ivyea_home, monkeypatch):
    from ivyea_agent import feishu_client

    monkeypatch.delenv("IVYEA_FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("IVYEA_FEISHU_APP_SECRET", raising=False)
    assert feishu_client.is_configured() is False
    assert feishu_client.verify()["ok"] is False


def test_notify_channel_reports_missing_creds(ivyea_home, monkeypatch):
    from ivyea_agent import notify, feishu_client

    monkeypatch.setattr(feishu_client, "is_configured", lambda: False)
    r = notify.send("x", channel="feishu_app")
    assert not r["ok"] and "凭据" in r["error"]


def test_notify_feishu_app_sends_card(ivyea_home, monkeypatch):
    from ivyea_agent import notify, feishu_client

    sent = []
    monkeypatch.setattr(feishu_client, "is_configured", lambda: True)
    monkeypatch.setattr(feishu_client, "default_chat_id", lambda: "oc_d")
    monkeypatch.setattr(feishu_client, "send_card",
                        lambda chat, card: sent.append((chat, card)) or "om_5")
    r = notify.send("正文", title="标题", channel="feishu_app")
    assert r["ok"] and r["message_id"] == "om_5"
    assert sent[0][0] == "oc_d"
    assert "卡片已发送" in notify.render_result(r)


def test_notify_redacts_before_sending(ivyea_home, monkeypatch):
    from ivyea_agent import notify, feishu_client

    sent = []
    monkeypatch.setattr(feishu_client, "is_configured", lambda: True)
    monkeypatch.setattr(feishu_client, "default_chat_id", lambda: "oc_d")
    monkeypatch.setattr(feishu_client, "send_card",
                        lambda chat, card: sent.append(card) or "om_6")
    notify.send("token=abc123xyz", channel="feishu_app")
    assert "abc123xyz" not in json.dumps(sent[0], ensure_ascii=False)


def test_long_message_is_chunked_into_multiple_cards(ivyea_home, monkeypatch):
    from ivyea_agent import notify, feishu_client

    sent = []
    monkeypatch.setattr(feishu_client, "is_configured", lambda: True)
    monkeypatch.setattr(feishu_client, "default_chat_id", lambda: "oc_d")
    monkeypatch.setattr(feishu_client, "send_card",
                        lambda chat, card: sent.append(card) or f"om_{len(sent)}")
    notify.send("行\n" * 5000, channel="feishu_app")
    assert len(sent) > 1, "超长正文必须分多张卡片，不能被飞书截断"
