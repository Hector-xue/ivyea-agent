"""按轮次切主脑（IvyeaOps 任务台的模型选择器）+ 任意中转商的模型清单。

这条路的铁律只有一条：**不传 model 时，行为与改动前逐字一致**。所以每个用例都
成对写 —— 一半验覆盖真的生效，一半验没覆盖时什么都没变。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture()
def svc(ivyea_home):
    from ivyea_agent import service
    return service


# ── 解析 ────────────────────────────────────────────────────────────────────

def test_no_model_field_keeps_global_config(svc):
    """不传 model：拿到的就是全局配置，且 overridden=False。"""
    from ivyea_agent import config

    cfg, key, overridden = svc._turn_model_config({"message": "hi"})
    assert overridden is False
    assert cfg == config.get_model_config()
    assert key == config.get_active_key()


def test_override_resolves_provider_and_key(svc, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    cfg, key, overridden = svc._turn_model_config({"model": "openrouter:x-ai/grok-4.6"})
    assert overridden is True
    assert key == "sk-test"
    assert cfg["provider_id"] == "openrouter"
    assert cfg["model"] == "x-ai/grok-4.6"
    assert cfg["base_url"] == "https://openrouter.ai/api/v1"
    assert cfg["key_env"] == "OPENROUTER_API_KEY"


def test_unknown_model_id_raises(svc):
    with pytest.raises(svc.ModelOverrideError) as got:
        svc._turn_model_config({"model": "nosuchvendor:nosuchmodel"})
    assert got.value.code == "unknown_model"


def test_missing_key_raises_instead_of_falling_back(svc, monkeypatch):
    """没配 key 时**报错**，绝不悄悄回落到主脑 —— 回落就是个假开关。"""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(svc.ModelOverrideError) as got:
        svc._turn_model_config({"model": "openrouter:x-ai/grok-4.6"})
    assert got.value.code == "model_key_missing"


def test_missing_key_tolerated_when_caller_brings_provider(svc, monkeypatch):
    """调用方自带 provider 实例时密钥根本用不上，不该因此拦下这一轮。"""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg, key, overridden = svc._turn_model_config(
        {"model": "openrouter:x-ai/grok-4.6"}, allow_keyless=True)
    assert overridden is True and key == ""
    assert cfg["provider_id"] == "openrouter"


def test_custom_without_base_url_raises(svc, monkeypatch):
    """custom 没地址时必须报错。放过去的话 from_settings 会回落到 DeepSeek 的地址，
    变成拿着 A 家的 key 打 B 家的接口。"""
    monkeypatch.setenv("CUSTOM_API_KEY", "sk-test")
    with pytest.raises(svc.ModelOverrideError) as got:
        svc._turn_model_config({"model": "custom:some-model"})
    assert got.value.code == "base_url_required"


def test_custom_inherits_base_url_from_active_settings(svc, monkeypatch):
    """当前主脑就是同一个 custom 端点时，沿用它已经配好的地址。"""
    from ivyea_agent import config

    monkeypatch.setenv("CUSTOM_API_KEY", "sk-test")
    s = config.load_settings()
    s.update({"provider": "custom", "provider_id": "custom",
              "base_url": "https://relay.example.com/v1", "key_env": "CUSTOM_API_KEY"})
    config.save_settings(s)
    cfg, _key, overridden = svc._turn_model_config({"model": "custom:some-model"})
    assert overridden is True
    assert cfg["base_url"] == "https://relay.example.com/v1"


# ── 接进 chat_stream ────────────────────────────────────────────────────────

class _EchoProvider:
    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        yield {"type": "text", "text": "好的。"}
        yield {"type": "final", "content": "好的。", "tool_calls": [], "usage": {}}

    def chat(self, messages, tools=None):
        self.calls += 1
        return {"content": "好的。", "tool_calls": []}


def _stream(svc, **payload):
    provider = _EchoProvider()
    events: list[tuple[str, dict]] = []
    body = {"message": "你好", "persist": False, "max_steps": 2, **payload}
    result = svc.chat_stream(body, lambda e, d: events.append((e, d)), provider=provider)
    return result, provider, events


def test_stream_without_model_reports_global_model(svc):
    """回归保护：不传 model 时 start 事件里的模型信息与 /health 逐字相同。"""
    _result, provider, events = _stream(svc)
    start = next(d for e, d in events if e == "start")
    assert start["model"] == svc.health()["model"]
    assert provider.calls == 1


def test_stream_with_bad_model_errors_without_calling_provider(svc):
    """选了个用不了的模型：当场报错，不能拿主脑顶上去跑完一轮。"""
    result, provider, events = _stream(svc, model="nosuchvendor:nosuchmodel")
    assert result["ok"] is False
    assert result["error"] == "unknown_model"
    assert [e for e, _d in events] == ["error"]
    assert provider.calls == 0


def test_stream_with_model_reports_that_model(svc, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _result, provider, events = _stream(svc, model="openrouter:x-ai/grok-4.6")
    start = next(d for e, d in events if e == "start")
    assert start["model"]["model"] == "x-ai/grok-4.6"
    assert start["model"]["provider"] == "openrouter"
    assert provider.calls == 1


def test_run_with_bad_model_errors(svc):
    out = svc.chat_run({"message": "你好", "persist": False, "model": "custom:x"},
                       provider=_EchoProvider())
    assert out["ok"] is False and out["error"] == "base_url_required"


# ── 任意中转商的模型清单 ────────────────────────────────────────────────────

def test_catalog_for_ad_hoc_relay(svc, monkeypatch, ivyea_home):
    """中转商不在内置 provider 表里：给 base_url + key 就该能列出模型。"""
    seen: dict[str, str] = {}

    def _fake(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization") or ""

        class _R:
            def read(self_inner):
                return json.dumps({"data": [{"id": "relay-a"}, {"id": "relay-b"}]}).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    out = svc.model_catalog({"provider": "apimart", "base_url": "https://api.apimart.ai/v1",
                             "api_key": "sk-relay", "refresh": True})
    assert out["ok"] is True
    assert out["catalog"]["models"] == ["relay-a", "relay-b"]
    assert seen["url"] == "https://api.apimart.ai/v1/models"
    assert seen["auth"] == "Bearer sk-relay"


def test_catalog_without_base_url_is_actionable(svc):
    out = svc.model_catalog({"provider": "nosuchrelay"})
    assert out["ok"] is False and out["error"] == "base_url_required"
    assert "Base URL" in out["catalog"]["error"]


def test_catalog_live_failure_falls_back_to_builtin(svc, monkeypatch):
    """拉不到清单（余额不足这类 402）时给内置清单兜底，并把原因带回去 ——
    面板必须还能打开，让人手输模型名。"""
    import io
    import urllib.error

    def _boom(req, timeout=0):
        # fp 必须给：HTTPError.read() 会去读它，传 None 的话炸的是测试自己。
        raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {},
                                     io.BytesIO(b'{"error":"insufficient balance"}'))

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    out = svc.model_catalog({"provider": "deepseek", "refresh": True})
    assert out["ok"] is True
    assert out["catalog"]["source"] == "builtin"
    assert out["catalog"]["models"]          # 内置清单还在
    assert "402" in out["catalog"]["error"]


def test_catalog_rejects_non_http_base_url(svc):
    """base_url 是调用方现给的，而取清单是**服务端**去访问它 ——
    file:// 这种 scheme 放过去等于开了个任意读本机文件的口子。"""
    out = svc.model_catalog({"provider": "custom", "base_url": "file:///etc/passwd"})
    assert out["ok"] is False and out["error"] == "base_url_invalid"
