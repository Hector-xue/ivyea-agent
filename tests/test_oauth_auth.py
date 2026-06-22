"""OAuth/Bearer token store for model providers."""
from __future__ import annotations

import os as _os
import stat
import time

import pytest


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.responses.pop(0)


def test_oauth_token_roundtrip(ivyea_home):
    from ivyea_agent import oauth_auth
    oauth_auth.set_auth_token("qwen-oauth", "tok", refresh_token="ref", expires_at=time.time() + 3600)
    assert oauth_auth.get_token("qwen-oauth") == "tok"
    assert oauth_auth.token_status("qwen-oauth") == "authenticated+refresh"
    assert oauth_auth.clear_auth("qwen-oauth") is True
    assert oauth_auth.get_token("qwen-oauth") == ""
    assert oauth_auth.clear_auth("qwen-oauth") is False


def test_oauth_token_expired(ivyea_home):
    from ivyea_agent import oauth_auth
    oauth_auth.set_auth_token("qwen-oauth", "tok", expires_at=time.time() - 1)
    assert oauth_auth.token_status("qwen-oauth") == "expired"


def test_import_qwen_cli_tokens(ivyea_home, tmp_path):
    from ivyea_agent import oauth_auth
    src = tmp_path / "oauth_creds.json"
    src.write_text(
        '{"access_token":"tok","refresh_token":"ref","expiry_date":1893456000000}',
        encoding="utf-8",
    )
    assert oauth_auth.import_qwen_cli_tokens(src) == src
    item = oauth_auth.get_auth("qwen-oauth")
    assert item["access_token"] == "tok"
    assert item["refresh_token"] == "ref"
    assert item["expires_at"] == 1893456000
    assert item["source"].startswith("qwen-cli:")


def test_qwen_cli_auth_path_respects_home_env(ivyea_home, tmp_path, monkeypatch):
    from ivyea_agent import oauth_auth
    monkeypatch.setenv("HOME", str(tmp_path))
    assert oauth_auth.qwen_cli_auth_path() == tmp_path / ".qwen" / "oauth_creds.json"


def test_qwen_cli_login_runs_command_and_imports(ivyea_home, tmp_path, monkeypatch):
    from ivyea_agent import oauth_auth
    qwen_dir = tmp_path / ".qwen"
    qwen_dir.mkdir()
    (qwen_dir / "oauth_creds.json").write_text(
        '{"access_token":"tok","refresh_token":"ref","expiry_date":1893456000000}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(oauth_auth.shutil, "which", lambda name: "/usr/bin/qwen")
    calls = {}

    class _Done:
        returncode = 0

    def fake_run(cmd, check=False, timeout=None):
        calls["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(oauth_auth.subprocess, "run", fake_run)
    assert oauth_auth.qwen_cli_login() == qwen_dir / "oauth_creds.json"
    assert calls["cmd"] == ["/usr/bin/qwen", "auth", "qwen-oauth"]
    assert oauth_auth.get_token("qwen-oauth") == "tok"


def test_qwen_refresh_updates_auth_store(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth
    oauth_auth.set_auth_token("qwen-oauth", "old", refresh_token="ref", expires_at=time.time() - 1)

    calls = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        calls["url"] = url
        calls["data"] = data
        return _Resp(payload={"access_token": "new", "refresh_token": "ref2", "expires_in": 3600})

    monkeypatch.setattr(oauth_auth.httpx, "post", fake_post)
    assert oauth_auth.refresh_qwen_token() == "new"
    item = oauth_auth.get_auth("qwen-oauth")
    assert item["access_token"] == "new"
    assert item["refresh_token"] == "ref2"
    assert item["expires_at"] > int(time.time())
    assert calls["data"]["grant_type"] == "refresh_token"
    assert calls["data"]["client_id"] == oauth_auth.QWEN_OAUTH_CLIENT_ID


def test_qwen_device_code_login_stores_tokens(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth
    responses = [
        _Resp(payload={
            "device_code": "dev",
            "user_code": "ABCD",
            "verification_uri_complete": "https://chat.qwen.ai/activate?user_code=ABCD",
            "expires_in": 60,
        }),
        _Resp(status_code=400, payload={"error": "authorization_pending"}),
        _Resp(payload={"access_token": "qwen-token", "refresh_token": "qwen-refresh", "expires_in": 3600}),
    ]
    calls = []

    def fake_post(url, headers=None, data=None, timeout=None):
        calls.append((url, data))
        return responses.pop(0)

    monkeypatch.setattr(oauth_auth.httpx, "post", fake_post)
    monkeypatch.setattr(oauth_auth.time, "sleep", lambda *_: None)
    monkeypatch.setattr(oauth_auth.webbrowser, "open", lambda *a, **k: None)
    seen = []
    oauth_auth.qwen_device_code_login(max_wait=10, notify=seen.append)
    item = oauth_auth.get_auth("qwen-oauth")
    assert item["access_token"] == "qwen-token"
    assert item["refresh_token"] == "qwen-refresh"
    assert item["source"] == "qwen-device-code"
    assert calls[0][0] == oauth_auth.QWEN_OAUTH_DEVICE_CODE_URL
    assert calls[0][1]["scope"] == oauth_auth.QWEN_OAUTH_SCOPE
    assert calls[1][1]["grant_type"] == oauth_auth.QWEN_OAUTH_DEVICE_GRANT_TYPE
    assert any("Qwen OAuth" in text for text in seen)


def test_get_active_key_refreshes_qwen_oauth(ivyea_home, monkeypatch):
    from ivyea_agent import config, models, oauth_auth
    oauth_auth.set_auth_token("qwen-oauth", "old", refresh_token="ref", expires_at=time.time() - 1)
    config.apply_model(models.by_id("qwen-oauth:qwen3-coder-plus"))
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(
        payload={"access_token": "new", "refresh_token": "ref", "expires_in": 3600}
    ))
    assert config.get_active_key() == "new"


def test_codex_refresh_updates_auth_store(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth
    oauth_auth.set_auth_token("openai-codex", "old", refresh_token="ref", expires_at=time.time() - 1)

    def fake_post(url, headers=None, data=None, timeout=None):
        return _Resp(payload={"access_token": "codex-new", "refresh_token": "codex-ref", "expires_in": 3600})

    monkeypatch.setattr(oauth_auth.httpx, "post", fake_post)
    assert oauth_auth.refresh_codex_token() == "codex-new"
    item = oauth_auth.get_auth("openai-codex")
    assert item["access_token"] == "codex-new"
    assert item["refresh_token"] == "codex-ref"
    assert item["expires_at"] > int(time.time())


def test_get_active_key_refreshes_codex(ivyea_home, monkeypatch):
    from ivyea_agent import config, models, oauth_auth
    oauth_auth.set_auth_token("openai-codex", "old", refresh_token="ref", expires_at=time.time() - 1)
    config.apply_model(models.by_id("openai-codex:gpt-5-codex"))
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(
        payload={"access_token": "codex-new", "refresh_token": "ref", "expires_in": 3600}
    ))
    assert config.get_active_key() == "codex-new"


def test_codex_device_code_login_stores_tokens(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth
    clients = [
        _Client([_Resp(payload={"user_code": "ABCD", "device_auth_id": "dev", "interval": 3})]),
        _Client([_Resp(payload={"authorization_code": "code", "code_verifier": "verifier"})]),
    ]

    def fake_client(*args, **kwargs):
        return clients.pop(0)

    monkeypatch.setattr(oauth_auth.httpx, "Client", fake_client)
    monkeypatch.setattr(oauth_auth.time, "sleep", lambda *_: None)
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(
        payload={"access_token": "codex-token", "refresh_token": "codex-refresh", "expires_in": 3600}
    ))
    seen = []
    oauth_auth.codex_device_code_login(max_wait=10, notify=seen.append)
    item = oauth_auth.get_auth("openai-codex")
    assert item["access_token"] == "codex-token"
    assert item["refresh_token"] == "codex-refresh"
    assert "ABCD" in seen[0]


def test_copilot_token_validation():
    from ivyea_agent import oauth_auth
    assert oauth_auth.validate_copilot_github_token("gho_x")[0] is True
    assert oauth_auth.validate_copilot_github_token("github_pat_x")[0] is True
    assert oauth_auth.validate_copilot_github_token("ghu_x")[0] is True
    valid, reason = oauth_auth.validate_copilot_github_token("ghp_x")
    assert valid is False
    assert "classic PAT" in reason


def test_copilot_exchange(monkeypatch):
    from ivyea_agent import oauth_auth
    oauth_auth.COPILOT_TOKEN_CACHE.clear()

    def fake_get(url, headers=None, timeout=None):
        assert headers["Authorization"] == "token gho_raw"
        return _Resp(payload={"token": "copilot-api", "expires_at": time.time() + 3600})

    monkeypatch.setattr(oauth_auth.httpx, "get", fake_get)
    token, expires_at = oauth_auth.exchange_copilot_token("gho_raw")
    assert token == "copilot-api"
    assert expires_at > time.time()


def test_resolve_copilot_api_token_from_env(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth
    monkeypatch.setenv("GH_TOKEN", "gho_raw")
    monkeypatch.setattr(oauth_auth, "exchange_copilot_token", lambda raw: ("copilot-api", time.time() + 3600))
    assert oauth_auth.resolve_copilot_api_token(strict=True) == "copilot-api"


def test_google_refresh_updates_auth_store(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth
    oauth_auth.set_auth_token("google-gemini-cli", "old", refresh_token="ref", expires_at=time.time() - 1)

    def fake_post(url, headers=None, data=None, timeout=None):
        assert data["grant_type"] == "refresh_token"
        assert data["client_id"] == oauth_auth.GOOGLE_OAUTH_CLIENT_ID
        return _Resp(payload={"access_token": "google-new", "refresh_token": "ref2", "expires_in": 3600})

    monkeypatch.setattr(oauth_auth.httpx, "post", fake_post)
    assert oauth_auth.refresh_google_token() == "google-new"
    item = oauth_auth.get_auth("google-gemini-cli")
    assert item["access_token"] == "google-new"
    assert item["refresh_token"] == "ref2"


def test_google_oauth_url_contains_pkce_params():
    from ivyea_agent import oauth_auth
    url = oauth_auth.google_oauth_url(
        redirect_uri="http://127.0.0.1:8085/oauth2callback",
        state="state",
        code_challenge="challenge",
    )
    assert "accounts.google.com" in url
    assert "code_challenge=challenge" in url
    assert "access_type=offline" in url


def test_exchange_google_code_stores_tokens(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth

    def fake_post(url, headers=None, data=None, timeout=None):
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "code"
        assert data["code_verifier"] == "verifier"
        return _Resp(payload={"access_token": "google-token", "refresh_token": "google-refresh", "expires_in": 3600})

    monkeypatch.setattr(oauth_auth.httpx, "post", fake_post)
    oauth_auth.exchange_google_code("code", "verifier", "http://127.0.0.1:8085/oauth2callback")
    item = oauth_auth.get_auth("google-gemini-cli")
    assert item["access_token"] == "google-token"
    assert item["refresh_token"] == "google-refresh"
    assert item["source"] == "google-oauth"


def test_google_oauth_login_paste_mode(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth

    monkeypatch.setattr(oauth_auth.webbrowser, "open", lambda *a, **k: None)
    monkeypatch.setattr(oauth_auth.httpx, "post", lambda *a, **k: _Resp(
        payload={"access_token": "google-token", "refresh_token": "google-refresh", "expires_in": 3600}
    ))
    seen = []
    oauth_auth.google_oauth_login(open_browser=False, callback_wait=0,
                                  notify=seen.append, prompt=lambda _: "code")
    assert any("Google OAuth" in s for s in seen)
    assert oauth_auth.get_token("google-gemini-cli") == "google-token"


def test_google_project_id_prefers_auth_metadata(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth

    monkeypatch.setenv("IVYEA_GEMINI_PROJECT_ID", "env-project")
    oauth_auth.set_google_project_id("saved-project")
    assert oauth_auth.google_project_id() == "saved-project"


def test_google_project_id_reads_gcloud_active_config(ivyea_home, tmp_path, monkeypatch):
    from ivyea_agent import oauth_auth

    gcloud = tmp_path / "gcloud"
    configs = gcloud / "configurations"
    configs.mkdir(parents=True)
    (gcloud / "active_config").write_text("work\n", encoding="utf-8")
    (configs / "config_work").write_text("[core]\nproject = gcloud-project\n", encoding="utf-8")
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(gcloud))
    for name in oauth_auth.GOOGLE_PROJECT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert oauth_auth.google_project_id() == "gcloud-project"


@pytest.mark.skipif(_os.name == "nt", reason="Windows 无 POSIX 文件权限位")
def test_oauth_file_permissions(ivyea_home):
    from ivyea_agent import oauth_auth
    oauth_auth.set_auth_token("qwen-oauth", "tok")
    mode = oauth_auth.auth_path().stat().st_mode
    assert stat.S_IMODE(mode) == 0o600
