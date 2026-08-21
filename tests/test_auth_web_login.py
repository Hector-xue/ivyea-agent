"""网页端登录订阅制 provider（Claude / Codex / Gemini / Qwen / Copilot）。

这条路存在的理由：这几家不是填 API key 而是要走 OAuth，原来只有 CLI 能做，
不会用命令行的人就被挡在门外。

铁律有三条，每条都在下面钉死：
  ① 凭据（verifier / state / device_code / token）**一律不出服务端**；
  ② start 立刻返回、不在 HTTP 请求里等用户；
  ③ 会话池带 TTL 和上限 —— 它装的是凭据。
"""
from __future__ import annotations

import json
import os as _os
import time

import pytest


class _Resp:
    """够真的假响应：**有 payload 就有 text**。

    真实的 httpx 响应不可能"有 JSON 体但 text 为空"，而生产代码里确实有
    `resp.json() if resp.text else {}` 这种判空写法 —— 假响应不还原这一点，
    测出来的就是假的。
    """

    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text if text is not None else (json.dumps(self._payload) if payload else "")
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _isolate_copilot_env():
    """copilot_login 会**直接写 os.environ**（为了让 token 当场生效，不必等重读 .env）。

    所以这里得自己存档还原。monkeypatch.delenv 在这儿不管用：变量原本不存在时它
    没什么可记，收尾也就不会把用例中途新写进去的那个删掉 —— 于是 token 留在进程
    环境里，串到后面 test_models 的 copilot probe 上（那个用例单独跑过、全量跑挂，
    典型的用例间污染）。
    """
    names = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
    saved = {n: _os.environ.get(n) for n in names}
    for n in names:
        _os.environ.pop(n, None)
    try:
        yield
    finally:
        for n, v in saved.items():
            if v is None:
                _os.environ.pop(n, None)
            else:
                _os.environ[n] = v


@pytest.fixture()
def svc(ivyea_home):
    from ivyea_agent import service
    service._AUTH_SESSIONS.clear()
    return service


# ── 状态 ────────────────────────────────────────────────────────────────────

def test_status_lists_every_subscription_provider_without_secrets(svc):
    from ivyea_agent import oauth_auth

    oauth_auth.set_auth_token("qwen-oauth", "super-secret-token",
                              refresh_token="secret-refresh", expires_at=time.time() + 3600)
    out = svc.auth_status()
    ids = {p["id"] for p in out["providers"]}
    assert ids == {"qwen-oauth", "openai-codex", "kimi-code",
                   "anthropic-oauth", "google-gemini-cli", "copilot"}
    qwen = next(p for p in out["providers"] if p["id"] == "qwen-oauth")
    assert qwen["ready"] is True and qwen["kind"] == "device"
    # 整个响应里不许出现 token
    assert "super-secret-token" not in repr(out)
    assert "secret-refresh" not in repr(out)


def test_unknown_provider_is_rejected(svc):
    with pytest.raises(ValueError):
        svc.auth_start("deepseek")          # 填 key 的那些不该走这条路


# ── 设备码流程（Qwen / Codex）────────────────────────────────────────────────

def test_device_start_returns_user_code_and_hides_credentials(svc, monkeypatch):
    from ivyea_agent import oauth_auth

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "device_code": "SECRET-DEVICE-CODE", "user_code": "ABCD-1234",
        "verification_uri_complete": "https://chat.qwen.ai/activate?user_code=ABCD-1234",
        "expires_in": 600,
    }))
    out = svc.auth_start("qwen-oauth")
    assert out["kind"] == "device"
    assert out["user_code"] == "ABCD-1234"
    assert out["session"]
    # device_code 和 PKCE verifier 是凭据：留在服务端，绝不回给调用方
    assert "SECRET-DEVICE-CODE" not in repr(out)
    assert "verifier" not in out


def test_device_poll_pending_then_ok(svc, monkeypatch):
    from ivyea_agent import oauth_auth

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "device_code": "dev", "user_code": "ABCD",
        "verification_uri_complete": "https://chat.qwen.ai/activate", "expires_in": 600,
    }))
    started = svc.auth_start("qwen-oauth")
    session = started["session"]

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k:
                        _Resp(status_code=400, payload={"error": "authorization_pending"}))
    assert svc.auth_poll("qwen-oauth", session)["status"] == "pending"

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "access_token": "qwen-token", "refresh_token": "qwen-refresh", "expires_in": 3600}))
    done = svc.auth_poll("qwen-oauth", session)
    assert done["status"] == "ok"
    assert oauth_auth.get_token("qwen-oauth") == "qwen-token"
    # 成功之后会话必须销毁：里面装着 device_code
    assert session not in svc._AUTH_SESSIONS


def test_device_poll_rejects_stale_session(svc):
    with pytest.raises(ValueError):
        svc.auth_poll("qwen-oauth", "nosuchsession")


def test_codex_device_flow(svc, monkeypatch):
    from ivyea_agent import oauth_auth

    monkeypatch.setattr(oauth_auth.httpx, "Client", lambda *a, **k: _Client([
        _Resp(payload={"user_code": "WXYZ", "device_auth_id": "dev", "interval": 3})]))
    started = svc.auth_start("openai-codex")
    assert started["user_code"] == "WXYZ"
    assert started["verification_uri"].endswith("/codex/device")

    # 403 = 用户还没在浏览器里确认，是"等待中"不是错误
    monkeypatch.setattr(oauth_auth.httpx, "Client", lambda *a, **k: _Client([_Resp(status_code=403)]))
    assert svc.auth_poll("openai-codex", started["session"])["status"] == "pending"

    monkeypatch.setattr(oauth_auth.httpx, "Client", lambda *a, **k: _Client([
        _Resp(payload={"authorization_code": "code", "code_verifier": "verifier"})]))
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "access_token": "codex-token", "refresh_token": "codex-refresh", "expires_in": 3600}))
    assert svc.auth_poll("openai-codex", started["session"])["status"] == "ok"
    assert oauth_auth.get_token("openai-codex") == "codex-token"


def test_device_provider_refuses_complete(svc, monkeypatch):
    from ivyea_agent import oauth_auth
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "device_code": "dev", "user_code": "A", "verification_uri_complete": "x", "expires_in": 60}))
    started = svc.auth_start("qwen-oauth")
    with pytest.raises(ValueError):
        svc.auth_complete("qwen-oauth", started["session"], "whatever")


# ── 粘码流程（Claude / Gemini）──────────────────────────────────────────────

def test_anthropic_paste_flow(svc, monkeypatch):
    from ivyea_agent import oauth_auth

    started = svc.auth_start("anthropic-oauth")
    assert started["kind"] == "paste"
    assert started["url"].startswith(oauth_auth.ANTHROPIC_OAUTH_AUTHORIZE_URL)
    # 授权链接可以给用户看，但 verifier/state 不行
    assert "verifier" not in started and "state" not in started

    state = svc._AUTH_SESSIONS[started["session"]]["ctx"]["state"]
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "access_token": "claude-token", "refresh_token": "claude-refresh", "expires_in": 3600}))
    out = svc.auth_complete("anthropic-oauth", started["session"], f"authcode#{state}")
    assert out["ok"] is True
    assert oauth_auth.get_token("anthropic-oauth") == "claude-token"
    assert started["session"] not in svc._AUTH_SESSIONS


def test_anthropic_rejects_wrong_state(svc, monkeypatch):
    """state 对不上就是粘错了或被篡改，必须拒绝而不是照换不误。"""
    from ivyea_agent import oauth_auth

    started = svc.auth_start("anthropic-oauth")
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={"access_token": "x"}))
    out = svc.auth_complete("anthropic-oauth", started["session"], "authcode#someone-elses-state")
    assert out["ok"] is False and "state" in out["error"]
    assert oauth_auth.get_token("anthropic-oauth") == ""


def test_google_paste_flow_reuses_the_same_redirect_uri(svc, monkeypatch):
    """换 token 用的 redirect_uri 必须和授权时**逐字一致**，否则 Google 直接拒。
    远程用的时候那个回调地址根本连不上（指的是用户自己的电脑），但它仍然要一字不差。"""
    from ivyea_agent import oauth_auth

    started = svc.auth_start("google-gemini-cli")
    used = started["url"]
    assert "redirect_uri=http" in used.replace("%3A", ":").replace("%2F", "/") or "redirect_uri=" in used

    seen = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        seen["data"] = dict(data or {})
        return _Resp(payload={"access_token": "g-token", "refresh_token": "g-refresh", "expires_in": 3600})

    monkeypatch.setattr(oauth_auth.httpx, "post", fake_post)
    ctx_uri = svc._AUTH_SESSIONS[started["session"]]["ctx"]["redirect_uri"]
    out = svc.auth_complete("google-gemini-cli", started["session"],
                            f"{ctx_uri}?state=x&code=the-code")
    assert out["ok"] is True
    assert seen["data"]["code"] == "the-code"
    assert seen["data"]["redirect_uri"] == ctx_uri
    assert oauth_auth.get_token("google-gemini-cli") == "g-token"


# ── Copilot：只是一个 GitHub token ──────────────────────────────────────────

def test_copilot_login_writes_its_own_env_var_not_gh_token(svc, monkeypatch):
    """**绝不能写 GH_TOKEN / GITHUB_TOKEN** —— 那是 gh CLI 和 CI 脚本在用的通用变量，
    往里塞一个 Copilot 专用 token 会连带影响它们，出问题还查不出是谁改的。"""
    from ivyea_agent import config, oauth_auth

    monkeypatch.setattr(oauth_auth, "exchange_copilot_token",
                        lambda tok, timeout=10.0: ("copilot-api-token", time.time() + 1800))
    started = svc.auth_start("copilot")
    assert started["kind"] == "token"
    out = svc.auth_complete("copilot", started["session"], "gho_realtoken")
    assert out["ok"] is True

    env_text = (config.IVYEA_DIR / ".env").read_text(encoding="utf-8")
    assert "COPILOT_GITHUB_TOKEN=gho_realtoken" in env_text
    assert "GH_TOKEN" not in env_text
    assert "\nGITHUB_TOKEN" not in env_text


def test_copilot_rejects_classic_pat(svc, monkeypatch):
    out = svc.auth_complete("copilot", "", "ghp_classic")
    assert out["ok"] is False and "ghp_" in out["error"]


def test_copilot_logout_only_clears_its_own_var(svc, monkeypatch):
    from ivyea_agent import config, oauth_auth

    monkeypatch.setattr(oauth_auth, "exchange_copilot_token",
                        lambda tok, timeout=10.0: ("copilot-api-token", time.time() + 1800))
    config.set_env_key("GH_TOKEN", "someone-elses-token")
    svc.auth_complete("copilot", "", "gho_realtoken")
    svc.auth_logout("copilot")

    env_text = (config.IVYEA_DIR / ".env").read_text(encoding="utf-8")
    assert "COPILOT_GITHUB_TOKEN=gho_realtoken" not in env_text
    assert "GH_TOKEN=someone-elses-token" in env_text     # 别人的东西一个字没动
    assert oauth_auth.get_token("copilot") == ""


# ── 会话池 ──────────────────────────────────────────────────────────────────

def test_sessions_expire(svc, monkeypatch):
    from ivyea_agent import oauth_auth

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "device_code": "dev", "user_code": "A", "verification_uri_complete": "x", "expires_in": 600}))
    started = svc.auth_start("qwen-oauth")
    svc._AUTH_SESSIONS[started["session"]]["created"] = time.time() - svc._AUTH_SESSION_TTL - 1
    with pytest.raises(ValueError):
        svc.auth_poll("qwen-oauth", started["session"])


def test_session_pool_is_capped(svc):
    """会话池装的是凭据，不能无限长。"""
    for _ in range(svc._AUTH_SESSIONS_MAX + 5):
        svc._auth_put("anthropic-oauth", {"verifier": "v", "state": "s"})
    assert len(svc._AUTH_SESSIONS) <= svc._AUTH_SESSIONS_MAX


# ── 轮询期间的暂时性故障 ────────────────────────────────────────────────────

def test_gateway_5xx_during_poll_is_retryable_not_fatal(svc, monkeypatch):
    """实测撞到过阿里云网关的 504。把它当硬失败，用户正站在授权页面前面却被告知
    "登录失败"，而其实什么都没坏 —— 会话销毁后还得从头再来一遍。"""
    from ivyea_agent import oauth_auth

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "device_code": "dev", "user_code": "A", "verification_uri_complete": "x", "expires_in": 600}))
    started = svc.auth_start("qwen-oauth")

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k:
                        _Resp(status_code=504, text="<html>504</html>"))
    out = svc.auth_poll("qwen-oauth", started["session"])
    assert out["status"] == "pending"
    assert "504" in out["note"]                       # 要说清楚在等什么
    assert started["session"] in svc._AUTH_SESSIONS   # 会话必须还在

    # 网关恢复后照常完成
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "access_token": "qwen-token", "refresh_token": "r", "expires_in": 3600}))
    assert svc.auth_poll("qwen-oauth", started["session"])["status"] == "ok"


def test_endless_gateway_failure_eventually_gives_up(svc, monkeypatch):
    """一直重试也不行的时候要认输，不能让界面永远转圈。"""
    from ivyea_agent import oauth_auth

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "device_code": "dev", "user_code": "A", "verification_uri_complete": "x", "expires_in": 600}))
    started = svc.auth_start("qwen-oauth")
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(status_code=502))
    last = {}
    for _ in range(oauth_auth._POLL_MAX_CONSECUTIVE_FAILS + 2):
        last = svc.auth_poll("qwen-oauth", started["session"])
        if last["status"] == "error":
            break
    assert last["status"] == "error" and "502" in last["error"]


def test_transient_failure_counter_resets_on_a_normal_answer(svc, monkeypatch):
    """对面答得好好的（authorization_pending），之前那点抖动就不该再算数。"""
    from ivyea_agent import oauth_auth

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "device_code": "dev", "user_code": "A", "verification_uri_complete": "x", "expires_in": 600}))
    started = svc.auth_start("qwen-oauth")
    ctx = svc._AUTH_SESSIONS[started["session"]]["ctx"]

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(status_code=503))
    svc.auth_poll("qwen-oauth", started["session"])
    assert ctx["fails"] == 1

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k:
                        _Resp(status_code=400, payload={"error": "authorization_pending"}))
    out = svc.auth_poll("qwen-oauth", started["session"])
    assert ctx["fails"] == 0 and "note" not in out


# ── Kimi Code 订阅（设备码）────────────────────────────────────────────────

def test_kimi_device_flow(svc, monkeypatch):
    """契约取自官方 CLI 包 @moonshot-ai/kimi-code：标准 RFC 8628。
    这里钉住三件事 —— 端点、client_id、grant_type，任何一个写错都只会在真机上炸。"""
    from ivyea_agent import oauth_auth

    seen: list = []

    def fake_post(url, headers=None, data=None, timeout=None):
        seen.append((url, dict(data or {})))
        if url.endswith("/device_authorization"):
            return _Resp(payload={"device_code": "SECRET-DEV", "user_code": "KIMI-1234",
                                  "verification_uri_complete": "https://auth.kimi.com/device?code=KIMI-1234",
                                  "expires_in": 900, "interval": 5})
        return _Resp(payload={"access_token": "kimi-token", "refresh_token": "kimi-refresh",
                              "expires_in": 3600})

    monkeypatch.setattr(oauth_auth.httpx, "post", fake_post)
    started = svc.auth_start("kimi-code")
    assert started["kind"] == "device"
    assert started["user_code"] == "KIMI-1234"
    assert "SECRET-DEV" not in repr(started)          # device_code 是凭据，不出服务端
    assert seen[0][0] == "https://auth.kimi.com/api/oauth/device_authorization"
    assert seen[0][1]["client_id"] == oauth_auth.KIMI_CODE_CLIENT_ID

    assert svc.auth_poll("kimi-code", started["session"])["status"] == "ok"
    assert seen[1][0] == "https://auth.kimi.com/api/oauth/token"
    assert seen[1][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert oauth_auth.get_token("kimi-code") == "kimi-token"


def test_kimi_pending_and_expired_are_told_apart(svc, monkeypatch):
    """authorization_pending 要继续等，expired_token 要当场说"过期了，重来"——
    混成一句"登录失败"，用户根本不知道该不该再点一次。"""
    from ivyea_agent import oauth_auth

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "device_code": "d", "user_code": "u", "verification_uri_complete": "x",
        "expires_in": 900, "interval": 5}))
    started = svc.auth_start("kimi-code")

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k:
                        _Resp(status_code=400, payload={"error": "authorization_pending"}))
    assert svc.auth_poll("kimi-code", started["session"])["status"] == "pending"

    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k:
                        _Resp(status_code=400, payload={"error": "expired_token"}))
    out = svc.auth_poll("kimi-code", started["session"])
    assert out["status"] == "error" and "过期" in out["error"]


def test_kimi_token_refreshes_when_expiring(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth
    import time as _t

    oauth_auth.set_auth_token("kimi-code", "old", refresh_token="ref", expires_at=_t.time() - 1)
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(payload={
        "access_token": "kimi-new", "refresh_token": "ref", "expires_in": 3600}))
    assert oauth_auth.resolve_provider_token("kimi-code") == "kimi-new"


# ── GLM Coding Plan 的地址 ──────────────────────────────────────────────────

def test_glm_coding_plan_uses_the_coding_only_endpoint():
    """官方明确要求 Coding Plan 用 `/api/coding/paas/v4`，填成通用的 `/api/paas/v4`
    会不通，而报错完全指不到"地址错了"上。两种条目并存、各走各的。"""
    from ivyea_agent import models

    assert models.provider_by_id("zai-coding")["base"] == "https://api.z.ai/api/coding/paas/v4"
    assert models.provider_by_id("glm-coding")["base"] == "https://open.bigmodel.cn/api/coding/paas/v4"
    # 普通 API key 那两条不许被改成 coding 端点
    assert models.provider_by_id("zai")["base"] == "https://api.z.ai/api/paas/v4"
    assert models.provider_by_id("glm-legacy")["base"] == "https://open.bigmodel.cn/api/paas/v4"


def test_kimi_code_provider_is_oauth_and_needs_no_key():
    from ivyea_agent import models

    p = models.provider_by_id("kimi-code")
    assert p["auth_type"] == "oauth_external"
    assert p["base"] == "https://api.kimi.com/coding/v1"
    assert not p["key_env"]          # 订阅登录，不该再要一个 API key
