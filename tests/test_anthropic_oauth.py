"""Claude(Anthropic) 订阅版 OAuth 登录：URL/解析、token 交换/刷新、resolve 分派、provider OAuth 模式。"""
from __future__ import annotations

import sys
import types

import pytest

from ivyea_agent import oauth_auth as oa


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or ""
    def json(self):
        return self._payload


# ── URL / 解析 ──
def test_authorize_url_has_pkce_and_state():
    url = oa.anthropic_oauth_url(code_challenge="CHAL", state="ST")
    for frag in ("client_id=", "code_challenge=CHAL", "code_challenge_method=S256",
                 "state=ST", "response_type=code", "redirect_uri="):
        assert frag in url


def test_parse_callback_forms():
    assert oa._parse_anthropic_callback("abc#ST") == ("abc", "ST")
    assert oa._parse_anthropic_callback("  bare  ") == ("bare", "")
    code, state = oa._parse_anthropic_callback(
        "https://console.anthropic.com/oauth/code/callback?code=xyz&state=QQ")
    assert code == "xyz" and state == "QQ"


# ── token 交换（登录）──
def test_login_exchanges_and_stores(monkeypatch):
    captured = {}
    monkeypatch.setattr(oa, "set_auth_token",
                        lambda pid, tok, **kw: captured.update({"pid": pid, "tok": tok, **kw}))
    posted = {}

    def fake_post(url, **kw):
        posted["url"] = url; posted["json"] = kw.get("json")
        return _Resp(200, {"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600})
    monkeypatch.setattr(oa.httpx, "post", fake_post)

    oa.anthropic_oauth_login(notify=lambda s: None, prompt=lambda p: "thecode")
    assert posted["url"] == oa.ANTHROPIC_OAUTH_TOKEN_URL
    body = posted["json"]
    assert body["grant_type"] == "authorization_code" and body["code"] == "thecode"
    assert body["code_verifier"] and body["client_id"] == oa.ANTHROPIC_OAUTH_CLIENT_ID
    assert captured["pid"] == "anthropic-oauth" and captured["tok"] == "acc-1"
    assert captured["refresh_token"] == "ref-1"


def test_login_state_mismatch_raises(monkeypatch):
    monkeypatch.setattr(oa.httpx, "post", lambda *a, **k: _Resp(200, {"access_token": "x"}))
    # 粘回的 state 与内部生成的随机 state 必不相等 → 报错
    with pytest.raises(oa.OAuthAuthError):
        oa.anthropic_oauth_login(notify=lambda s: None, prompt=lambda p: "code#WRONGSTATE")


# ── 刷新 ──
def test_refresh_uses_refresh_token(monkeypatch):
    monkeypatch.setattr(oa, "get_auth", lambda pid: {"access_token": "old", "refresh_token": "ref-1", "source": "oauth"})
    captured = {}
    monkeypatch.setattr(oa, "set_auth_token", lambda pid, tok, **kw: captured.update({"tok": tok}))
    monkeypatch.setattr(oa.httpx, "post", lambda url, **kw: _Resp(200, {"access_token": "acc-2"}, text="{}"))
    assert oa.refresh_anthropic_token() == "acc-2" and captured["tok"] == "acc-2"


def test_resolve_provider_token_refreshes_when_expiring(monkeypatch):
    monkeypatch.setattr(oa, "get_auth", lambda pid: {"access_token": "old", "refresh_token": "r", "expires_at": 1})
    monkeypatch.setattr(oa, "refresh_anthropic_token", lambda: "REFRESHED")
    assert oa.resolve_provider_token("anthropic-oauth", "") == "REFRESHED"


# ── provider OAuth 模式 ──
def test_from_settings_builds_oauth_provider():
    from ivyea_agent.providers import base
    p = base.from_settings({"kind": "anthropic", "model": "claude-sonnet-4-6",
                            "api_mode": "anthropic_oauth", "auth_type": "oauth_external"}, "bearer-tok")
    assert p.oauth is True and p.name == "anthropic"


def test_oauth_client_uses_bearer_and_beta(monkeypatch):
    from ivyea_agent.providers.anthropic_provider import AnthropicProvider
    captured = {}
    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda **kw: captured.update(kw) or object()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    p = AnthropicProvider("bearer-tok", "claude-sonnet-4-6", oauth=True)
    p._cli()
    assert captured.get("auth_token") == "bearer-tok" and "api_key" not in captured
    assert captured["default_headers"]["anthropic-beta"] == oa.ANTHROPIC_OAUTH_BETA


def test_oauth_system_prepends_claude_code_identity():
    from ivyea_agent.providers.anthropic_provider import AnthropicProvider
    p = AnthropicProvider("t", "claude-sonnet-4-6", oauth=True)
    blocks = p._system("你是亚马逊运营 Agent")
    assert blocks[0]["text"].startswith("You are Claude Code")
    assert any("亚马逊" in b.get("text", "") for b in blocks)
    # 非 oauth 不注入身份
    p2 = AnthropicProvider("k", "claude-sonnet-4-6", oauth=False)
    b2 = p2._system("x")
    assert not any("Claude Code" in b.get("text", "") for b in (b2 or []))


# ── 登记 ──
def test_models_entry_registered():
    from ivyea_agent import models
    e = models.provider_by_id("anthropic-oauth")
    assert e and e["kind"] == "anthropic" and e["api_mode"] == "anthropic_oauth"
    assert e["auth_type"] == "oauth_external"
    caps = models.provider_capabilities(e)
    assert caps["tools"] and caps["streaming"] and caps["oauth"]
