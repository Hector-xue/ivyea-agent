"""Local OAuth/Bearer token storage for model providers.

Tokens live in ``~/.ivyea/auth.json`` instead of ``.env`` because they may
include refresh metadata and should be managed as login state rather than
ordinary API keys.
"""
from __future__ import annotations

import json
import os
import time
import base64
import hashlib
import http.server
import configparser
import secrets
import shutil
import subprocess
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx


class OAuthAuthError(Exception):
    pass


QWEN_OAUTH_CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"
QWEN_OAUTH_DEVICE_CODE_URL = "https://chat.qwen.ai/api/v1/oauth2/device/code"
QWEN_OAUTH_TOKEN_URL = "https://chat.qwen.ai/api/v1/oauth2/token"
QWEN_OAUTH_SCOPE = "openid profile email model.completion"
QWEN_OAUTH_DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
QWEN_REFRESH_SKEW_SECONDS = 120
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_AUTH_ISSUER = "https://auth.openai.com"
CODEX_REFRESH_SKEW_SECONDS = 120
COPILOT_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
COPILOT_TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_UNSUPPORTED_PREFIX = "ghp_"
COPILOT_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
GOOGLE_OAUTH_CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile"
)
GOOGLE_REFRESH_SKEW_SECONDS = 60
GOOGLE_REDIRECT_HOST = "127.0.0.1"
GOOGLE_REDIRECT_PATH = "/oauth2callback"
GOOGLE_REDIRECT_PORT = 8085
GOOGLE_PROJECT_ENV_VARS = ("IVYEA_GEMINI_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCP_PROJECT")


def _auth_file() -> Path:
    from . import config
    return config.IVYEA_DIR / "auth.json"


def _ensure_parent() -> None:
    from . import config
    config.ensure_dirs()


def load_auth() -> dict[str, Any]:
    path = _auth_file()
    if not path.exists():
        return {"providers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"providers": {}}
    if not isinstance(data, dict):
        return {"providers": {}}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        data["providers"] = {}
    return data


def save_auth(data: dict[str, Any]) -> None:
    _ensure_parent()
    path = _auth_file()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def set_auth_token(provider_id: str, token: str, *,
                   refresh_token: str = "", expires_at: int | float = 0,
                   source: str = "manual") -> None:
    if not provider_id:
        raise ValueError("provider_id required")
    if not token:
        raise ValueError("token required")
    data = load_auth()
    previous = data.setdefault("providers", {}).get(provider_id, {})
    metadata = previous.get("metadata") if isinstance(previous, dict) else {}
    data.setdefault("providers", {})[provider_id] = {
        "access_token": token,
        "refresh_token": refresh_token,
        "expires_at": int(float(expires_at or 0)),
        "source": source,
        "updated_at": int(time.time()),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    save_auth(data)


def set_auth_metadata(provider_id: str, **metadata: Any) -> None:
    if not provider_id:
        raise ValueError("provider_id required")
    cleaned = {str(k): v for k, v in metadata.items() if v is not None}
    data = load_auth()
    item = data.setdefault("providers", {}).setdefault(provider_id, {})
    if not isinstance(item, dict):
        item = {}
        data["providers"][provider_id] = item
    meta = item.setdefault("metadata", {})
    if not isinstance(meta, dict):
        meta = {}
        item["metadata"] = meta
    meta.update(cleaned)
    item["updated_at"] = int(time.time())
    save_auth(data)


def get_auth_metadata(provider_id: str) -> dict[str, Any]:
    meta = get_auth(provider_id).get("metadata", {})
    return meta if isinstance(meta, dict) else {}


def clear_auth(provider_id: str) -> bool:
    data = load_auth()
    providers = data.setdefault("providers", {})
    existed = provider_id in providers
    if existed:
        del providers[provider_id]
        save_auth(data)
    return existed


def get_auth(provider_id: str) -> dict[str, Any]:
    data = load_auth()
    item = data.get("providers", {}).get(provider_id, {})
    return item if isinstance(item, dict) else {}


def get_token(provider_id: str) -> str:
    token = get_auth(provider_id).get("access_token", "")
    return token if isinstance(token, str) else ""


def token_status(provider_id: str) -> str:
    item = get_auth(provider_id)
    if not item.get("access_token"):
        return "not-authenticated"
    expires_at = int(item.get("expires_at") or 0)
    if expires_at and expires_at <= int(time.time()):
        if item.get("refresh_token"):
            return "expired+refresh"
        return "expired"
    if item.get("refresh_token"):
        return "authenticated+refresh"
    return "authenticated"


def _is_expiring(expires_at: Any, skew_seconds: int = QWEN_REFRESH_SKEW_SECONDS) -> bool:
    try:
        expiry = int(float(expires_at or 0))
    except (TypeError, ValueError):
        return True
    if expiry <= 0:
        return False
    return int(time.time()) + max(0, int(skew_seconds)) >= expiry


def _expires_at_from_payload(payload: dict[str, Any], fallback: int = 0) -> int:
    expires_at = payload.get("expires_at") or payload.get("expiry_date") or 0
    try:
        direct = int(float(expires_at or 0))
    except (TypeError, ValueError):
        direct = 0
    if direct:
        return int(direct / 1000) if direct > 10_000_000_000 else direct
    try:
        expires_in = int(float(payload.get("expires_in") or 0))
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        return int(time.time()) + expires_in
    return fallback


def _jwt_expires_at(token: str) -> int:
    parts = token.split(".")
    if len(parts) < 2:
        return 0
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return 0
    try:
        return int(float(data.get("exp") or 0))
    except (TypeError, ValueError):
        return 0


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _google_client_id() -> str:
    return os.getenv("IVYEA_GEMINI_CLIENT_ID", GOOGLE_OAUTH_CLIENT_ID).strip()


def _google_client_secret() -> str:
    return os.getenv("IVYEA_GEMINI_CLIENT_SECRET", "").strip()


def _gcloud_config_dir() -> Path:
    return Path(os.getenv("CLOUDSDK_CONFIG") or (Path.home() / ".config" / "gcloud"))


def _gcloud_active_project() -> str:
    cfg_dir = _gcloud_config_dir()
    active = "default"
    active_file = cfg_dir / "active_config"
    try:
        raw_active = active_file.read_text(encoding="utf-8").strip()
    except OSError:
        raw_active = ""
    if raw_active:
        active = raw_active
    cfg_file = cfg_dir / "configurations" / f"config_{active}"
    parser = configparser.ConfigParser()
    try:
        parser.read(cfg_file, encoding="utf-8")
    except configparser.Error:
        return ""
    if parser.has_option("core", "project"):
        return parser.get("core", "project").strip()
    return ""


def google_project_id() -> str:
    meta_project = str(get_auth_metadata("google-gemini-cli").get("project_id") or "").strip()
    if meta_project:
        return meta_project
    for env_name in GOOGLE_PROJECT_ENV_VARS:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return _gcloud_active_project()


def set_google_project_id(project_id: str) -> None:
    set_auth_metadata("google-gemini-cli", project_id=project_id.strip())


def google_oauth_url(*, redirect_uri: str, state: str, code_challenge: str) -> str:
    params = {
        "client_id": _google_client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return GOOGLE_OAUTH_AUTH_URL + "?" + urllib.parse.urlencode(params)


def _extract_oauth_code(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(value)
        return (urllib.parse.parse_qs(parsed.query).get("code") or [""])[0]
    if value.startswith("?"):
        return (urllib.parse.parse_qs(value[1:]).get("code") or [""])[0]
    return value


def exchange_google_code(code: str, verifier: str, redirect_uri: str,
                         timeout: float = 20.0) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": _google_client_id(),
        "redirect_uri": redirect_uri,
    }
    secret = _google_client_secret()
    if secret:
        data["client_secret"] = secret
    try:
        response = httpx.post(
            GOOGLE_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data=data,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OAuthAuthError(f"Google Gemini OAuth code exchange failed: {exc}") from exc
    if response.status_code >= 400:
        body = response.text.strip()
        suffix = f" Response: {body[:200]}" if body else ""
        raise OAuthAuthError(f"Google Gemini OAuth code exchange failed with HTTP {response.status_code}.{suffix}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthAuthError(f"Google Gemini OAuth code exchange returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OAuthAuthError("Google Gemini OAuth code exchange returned invalid payload.")
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise OAuthAuthError("Google Gemini OAuth response missing access_token or refresh_token.")
    set_auth_token(
        "google-gemini-cli",
        access_token,
        refresh_token=refresh_token,
        expires_at=_expires_at_from_payload(payload),
        source="google-oauth",
    )
    return payload


class _GoogleOAuthHandler(http.server.BaseHTTPRequestHandler):
    expected_state = ""
    code = ""
    error = ""
    ready: threading.Event | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != GOOGLE_REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        if state != type(self).expected_state:
            type(self).error = "state_mismatch"
            self._html(400, "Ivyea Agent Google OAuth state mismatch. Return to terminal.")
        elif (params.get("error") or [""])[0]:
            type(self).error = (params.get("error") or [""])[0]
            self._html(400, "Ivyea Agent Google OAuth failed. Return to terminal.")
        else:
            type(self).code = (params.get("code") or [""])[0]
            self._html(200, "Ivyea Agent Google OAuth complete. You can close this tab.")
        if type(self).ready:
            type(self).ready.set()

    def _html(self, status: int, text: str) -> None:
        body = f"<!doctype html><meta charset='utf-8'><title>Ivyea Agent</title><p>{text}</p>".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _bind_google_oauth_server(port: int = GOOGLE_REDIRECT_PORT) -> tuple[http.server.HTTPServer, int]:
    try:
        server = http.server.HTTPServer((GOOGLE_REDIRECT_HOST, port), _GoogleOAuthHandler)
        return server, port
    except OSError:
        server = http.server.HTTPServer((GOOGLE_REDIRECT_HOST, 0), _GoogleOAuthHandler)
        return server, int(server.server_address[1])


def google_oauth_login(*, open_browser: bool = True, callback_wait: float = 300.0,
                       notify: Any = None, prompt: Any = None) -> None:
    emit = notify or print
    ask = prompt or input
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    if not open_browser:
        redirect_uri = f"http://{GOOGLE_REDIRECT_HOST}:{GOOGLE_REDIRECT_PORT}{GOOGLE_REDIRECT_PATH}"
        url = google_oauth_url(redirect_uri=redirect_uri, state=state, code_challenge=challenge)
        emit(f"打开浏览器完成 Google OAuth：{url}")
        emit("登录后浏览器会跳转到 localhost；复制完整 callback URL 或 code 参数粘贴回来。")
        code = _extract_oauth_code(str(ask("Callback URL or code: ")))
        if not code:
            raise OAuthAuthError("Google Gemini OAuth did not receive an authorization code.")
        exchange_google_code(code, verifier, redirect_uri)
        return
    try:
        server, port = _bind_google_oauth_server()
    except OSError:
        redirect_uri = f"http://{GOOGLE_REDIRECT_HOST}:{GOOGLE_REDIRECT_PORT}{GOOGLE_REDIRECT_PATH}"
        url = google_oauth_url(redirect_uri=redirect_uri, state=state, code_challenge=challenge)
        emit(f"无法启动本地回调服务，请手动打开：{url}")
        code = _extract_oauth_code(str(ask("Callback URL or code: ")))
        if not code:
            raise OAuthAuthError("Google Gemini OAuth did not receive an authorization code.")
        exchange_google_code(code, verifier, redirect_uri)
        return
    redirect_uri = f"http://{GOOGLE_REDIRECT_HOST}:{port}{GOOGLE_REDIRECT_PATH}"
    url = google_oauth_url(redirect_uri=redirect_uri, state=state, code_challenge=challenge)
    _GoogleOAuthHandler.expected_state = state
    _GoogleOAuthHandler.code = ""
    _GoogleOAuthHandler.error = ""
    _GoogleOAuthHandler.ready = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        emit(f"打开浏览器完成 Google OAuth：{url}")
        if open_browser:
            try:
                webbrowser.open(url, new=1, autoraise=True)
            except (OSError, RuntimeError):
                pass
        code = ""
        if _GoogleOAuthHandler.ready.wait(timeout=max(0.0, callback_wait)):
            if _GoogleOAuthHandler.error:
                raise OAuthAuthError(f"Google Gemini OAuth failed: {_GoogleOAuthHandler.error}")
            code = _GoogleOAuthHandler.code
        if not code:
            emit("如果浏览器无法回调，请粘贴完整 callback URL 或 code 参数。")
            code = _extract_oauth_code(str(ask("Callback URL or code: ")))
        if not code:
            raise OAuthAuthError("Google Gemini OAuth did not receive an authorization code.")
        exchange_google_code(code, verifier, redirect_uri)
    finally:
        try:
            server.shutdown()
            server.server_close()
        finally:
            thread.join(timeout=2.0)


def refresh_qwen_token(timeout: float = 20.0) -> str:
    item = get_auth("qwen-oauth")
    refresh_token = str(item.get("refresh_token") or "").strip()
    if not refresh_token:
        raise OAuthAuthError("Qwen OAuth refresh_token missing; re-import or re-authenticate first.")
    try:
        response = httpx.post(
            QWEN_OAUTH_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": QWEN_OAUTH_CLIENT_ID,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OAuthAuthError(f"Qwen OAuth refresh failed: {exc}") from exc
    if response.status_code >= 400:
        body = response.text.strip()
        suffix = f" Response: {body[:200]}" if body else ""
        raise OAuthAuthError(f"Qwen OAuth refresh failed with HTTP {response.status_code}.{suffix}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthAuthError(f"Qwen OAuth refresh returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OAuthAuthError("Qwen OAuth refresh returned invalid payload.")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise OAuthAuthError("Qwen OAuth refresh response missing access_token.")
    set_auth_token(
        "qwen-oauth",
        access_token,
        refresh_token=str(payload.get("refresh_token") or refresh_token).strip(),
        expires_at=_expires_at_from_payload(payload, int(item.get("expires_at") or 0)),
        source=str(item.get("source") or "qwen-oauth"),
    )
    return access_token


def qwen_device_code_login(*, timeout: float = 15.0, max_wait: float | None = None,
                           open_browser: bool = True, notify: Any = None) -> None:
    emit = notify or print
    verifier, challenge = _pkce_pair()
    try:
        response = httpx.post(
            QWEN_OAUTH_DEVICE_CODE_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "x-request-id": secrets.token_hex(16),
            },
            data={
                "client_id": QWEN_OAUTH_CLIENT_ID,
                "scope": QWEN_OAUTH_SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OAuthAuthError(f"Qwen OAuth device authorization failed: {exc}") from exc
    if response.status_code >= 400:
        body = response.text.strip()
        suffix = f" Response: {body[:200]}" if body else ""
        raise OAuthAuthError(f"Qwen OAuth device authorization failed with HTTP {response.status_code}.{suffix}")
    try:
        device = response.json()
    except ValueError as exc:
        raise OAuthAuthError(f"Qwen OAuth device authorization returned invalid JSON: {exc}") from exc
    if not isinstance(device, dict) or not device.get("device_code"):
        raise OAuthAuthError("Qwen OAuth device authorization returned invalid payload.")
    url = str(device.get("verification_uri_complete") or device.get("verification_uri") or "").strip()
    user_code = str(device.get("user_code") or "").strip()
    if url:
        emit(f"打开浏览器完成 Qwen OAuth：{url}")
        if open_browser:
            try:
                webbrowser.open(url, new=1, autoraise=True)
            except (OSError, RuntimeError):
                pass
    elif user_code:
        emit(f"打开 Qwen OAuth 页面并输入代码：{user_code}")
    else:
        raise OAuthAuthError("Qwen OAuth device authorization missing verification URL/user code.")
    try:
        expires_in = int(float(device.get("expires_in") or 600))
    except (TypeError, ValueError):
        expires_in = 600
    wait_budget = max_wait if max_wait is not None else float(expires_in)
    start = time.monotonic()
    poll_interval = 2.0
    while time.monotonic() - start < max(1.0, wait_budget):
        time.sleep(poll_interval)
        try:
            token_resp = httpx.post(
                QWEN_OAUTH_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={
                    "grant_type": QWEN_OAUTH_DEVICE_GRANT_TYPE,
                    "client_id": QWEN_OAUTH_CLIENT_ID,
                    "device_code": str(device.get("device_code") or ""),
                    "code_verifier": verifier,
                },
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise OAuthAuthError(f"Qwen OAuth device token poll failed: {exc}") from exc
        payload: dict[str, Any]
        try:
            parsed = token_resp.json()
            payload = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            payload = {}
        if token_resp.status_code == 400 and payload.get("error") == "authorization_pending":
            poll_interval = 2.0
            continue
        if token_resp.status_code == 429 and payload.get("error") == "slow_down":
            poll_interval = min(poll_interval * 1.5, 10.0)
            continue
        if token_resp.status_code >= 400:
            detail = payload.get("error_description") or payload.get("error") or token_resp.text[:200]
            raise OAuthAuthError(f"Qwen OAuth device token poll failed with HTTP {token_resp.status_code}: {detail}")
        access_token = str(payload.get("access_token") or "").strip()
        if access_token:
            set_auth_token(
                "qwen-oauth",
                access_token,
                refresh_token=str(payload.get("refresh_token") or "").strip(),
                expires_at=_expires_at_from_payload(payload),
                source="qwen-device-code",
            )
            return
    raise OAuthAuthError("Qwen OAuth device-code login timed out.")


def refresh_codex_token(timeout: float = 20.0) -> str:
    item = get_auth("openai-codex")
    refresh_token = str(item.get("refresh_token") or "").strip()
    if not refresh_token:
        raise OAuthAuthError("Codex OAuth refresh_token missing; run device-code login again.")
    try:
        response = httpx.post(
            CODEX_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CODEX_OAUTH_CLIENT_ID,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OAuthAuthError(f"Codex OAuth refresh failed: {exc}") from exc
    if response.status_code >= 400:
        body = response.text.strip()
        suffix = f" Response: {body[:200]}" if body else ""
        raise OAuthAuthError(f"Codex OAuth refresh failed with HTTP {response.status_code}.{suffix}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthAuthError(f"Codex OAuth refresh returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OAuthAuthError("Codex OAuth refresh returned invalid payload.")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise OAuthAuthError("Codex OAuth refresh response missing access_token.")
    set_auth_token(
        "openai-codex",
        access_token,
        refresh_token=str(payload.get("refresh_token") or refresh_token).strip(),
        expires_at=_expires_at_from_payload(payload, _jwt_expires_at(access_token)),
        source=str(item.get("source") or "device-code"),
    )
    return access_token


def refresh_google_token(timeout: float = 20.0) -> str:
    item = get_auth("google-gemini-cli")
    refresh_token = str(item.get("refresh_token") or "").strip()
    if not refresh_token:
        raise OAuthAuthError("Google Gemini OAuth refresh_token missing; run OAuth login or import credentials first.")
    try:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": os.getenv("IVYEA_GEMINI_CLIENT_ID", GOOGLE_OAUTH_CLIENT_ID),
        }
        client_secret = os.getenv("IVYEA_GEMINI_CLIENT_SECRET", "").strip()
        if client_secret:
            data["client_secret"] = client_secret
        response = httpx.post(
            GOOGLE_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data=data,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OAuthAuthError(f"Google Gemini OAuth refresh failed: {exc}") from exc
    if response.status_code >= 400:
        body = response.text.strip()
        suffix = f" Response: {body[:200]}" if body else ""
        raise OAuthAuthError(f"Google Gemini OAuth refresh failed with HTTP {response.status_code}.{suffix}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthAuthError(f"Google Gemini OAuth refresh returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OAuthAuthError("Google Gemini OAuth refresh returned invalid payload.")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise OAuthAuthError("Google Gemini OAuth refresh response missing access_token.")
    set_auth_token(
        "google-gemini-cli",
        access_token,
        refresh_token=str(payload.get("refresh_token") or refresh_token).strip(),
        expires_at=_expires_at_from_payload(payload, int(item.get("expires_at") or 0)),
        source=str(item.get("source") or "manual"),
    )
    return access_token


def validate_copilot_github_token(token: str) -> tuple[bool, str]:
    cleaned = token.strip()
    if not cleaned:
        return False, "empty token"
    if cleaned.startswith(COPILOT_UNSUPPORTED_PREFIX):
        return False, "classic PAT(ghp_*) is not supported by Copilot API"
    return True, "OK"


def resolve_copilot_github_token() -> tuple[str, str]:
    from . import config
    config.load_env()
    for env_name in COPILOT_ENV_VARS:
        token = os.environ.get(env_name, "").strip()
        if not token:
            continue
        valid, _ = validate_copilot_github_token(token)
        if valid:
            return token, env_name
    return "", ""


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def exchange_copilot_token(raw_token: str, timeout: float = 10.0) -> tuple[str, float]:
    raw_token = raw_token.strip()
    valid, reason = validate_copilot_github_token(raw_token)
    if not valid:
        raise OAuthAuthError(reason)
    fp = _fingerprint(raw_token)
    cached = COPILOT_TOKEN_CACHE.get(fp)
    if cached and time.time() < cached[1] - 120:
        return cached
    try:
        response = httpx.get(
            COPILOT_TOKEN_EXCHANGE_URL,
            headers={
                "Authorization": f"token {raw_token}",
                "User-Agent": "IvyeaAgent/1.0",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.104.1",
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OAuthAuthError(f"Copilot token exchange failed: {exc}") from exc
    if response.status_code >= 400:
        body = response.text.strip()
        suffix = f" Response: {body[:200]}" if body else ""
        raise OAuthAuthError(f"Copilot token exchange failed with HTTP {response.status_code}.{suffix}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthAuthError(f"Copilot token exchange returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OAuthAuthError("Copilot token exchange returned invalid payload.")
    api_token = str(payload.get("token") or "").strip()
    if not api_token:
        raise OAuthAuthError("Copilot token exchange returned empty token.")
    try:
        expires_at = float(payload.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= 0:
        expires_at = time.time() + 1800
    COPILOT_TOKEN_CACHE[fp] = (api_token, expires_at)
    return api_token, expires_at


def resolve_copilot_api_token(*, exchange: bool = True, strict: bool = False) -> str:
    raw, _ = resolve_copilot_github_token()
    if not raw:
        return get_token("copilot")
    if not exchange:
        return raw
    try:
        api_token, _ = exchange_copilot_token(raw)
    except OAuthAuthError:
        if strict:
            raise
        return raw
    return api_token


def codex_device_code_login(*, timeout: float = 15.0, max_wait: float = 15 * 60,
                            notify: Any = None) -> None:
    def emit(text: str) -> None:
        if notify:
            notify(text)

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.post(
                f"{CODEX_AUTH_ISSUER}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise OAuthAuthError(f"Failed to request Codex device code: {exc}") from exc
    if resp.status_code != 200:
        raise OAuthAuthError(f"Codex device code request returned HTTP {resp.status_code}.")
    try:
        device_data = resp.json()
    except ValueError as exc:
        raise OAuthAuthError(f"Codex device code response returned invalid JSON: {exc}") from exc
    user_code = str(device_data.get("user_code") or "").strip()
    device_auth_id = str(device_data.get("device_auth_id") or "").strip()
    try:
        poll_interval = max(3, int(device_data.get("interval") or 5))
    except (TypeError, ValueError):
        poll_interval = 5
    if not user_code or not device_auth_id:
        raise OAuthAuthError("Codex device code response missing user_code or device_auth_id.")
    emit(f"打开 {CODEX_AUTH_ISSUER}/codex/device 并输入代码：{user_code}")
    start = time.monotonic()
    code_resp: dict[str, Any] | None = None
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            while time.monotonic() - start < max_wait:
                time.sleep(poll_interval)
                poll = client.post(
                    f"{CODEX_AUTH_ISSUER}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    try:
                        code_resp = poll.json()
                    except ValueError as exc:
                        raise OAuthAuthError(f"Codex device polling returned invalid JSON: {exc}") from exc
                    break
                if poll.status_code in {403, 404}:
                    continue
                raise OAuthAuthError(f"Codex device polling returned HTTP {poll.status_code}.")
    except KeyboardInterrupt as exc:
        raise OAuthAuthError("Codex device-code login cancelled.") from exc
    if code_resp is None:
        raise OAuthAuthError("Codex device-code login timed out.")
    authorization_code = str(code_resp.get("authorization_code") or "").strip()
    code_verifier = str(code_resp.get("code_verifier") or "").strip()
    if not authorization_code or not code_verifier:
        raise OAuthAuthError("Codex device auth response missing authorization_code or code_verifier.")
    try:
        token_resp = httpx.post(
            CODEX_OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": f"{CODEX_AUTH_ISSUER}/deviceauth/callback",
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OAuthAuthError(f"Codex token exchange failed: {exc}") from exc
    if token_resp.status_code != 200:
        raise OAuthAuthError(f"Codex token exchange returned HTTP {token_resp.status_code}.")
    try:
        payload = token_resp.json()
    except ValueError as exc:
        raise OAuthAuthError(f"Codex token exchange returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OAuthAuthError("Codex token exchange returned invalid payload.")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise OAuthAuthError("Codex token exchange did not return access_token.")
    set_auth_token(
        "openai-codex",
        access_token,
        refresh_token=str(payload.get("refresh_token") or "").strip(),
        expires_at=_expires_at_from_payload(payload, _jwt_expires_at(access_token)),
        source="device-code",
    )


def resolve_provider_token(provider_id: str, env_name: str = "", *, refresh: bool = True) -> str:
    from . import config
    config.load_env()
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    if provider_id == "qwen-oauth":
        item = get_auth(provider_id)
        if refresh and item.get("access_token") and item.get("refresh_token") and _is_expiring(item.get("expires_at")):
            return refresh_qwen_token()
    if provider_id == "openai-codex":
        item = get_auth(provider_id)
        if refresh and item.get("access_token") and item.get("refresh_token") and _is_expiring(item.get("expires_at"), CODEX_REFRESH_SKEW_SECONDS):
            return refresh_codex_token()
    if provider_id == "google-gemini-cli":
        item = get_auth(provider_id)
        if refresh and item.get("access_token") and item.get("refresh_token") and _is_expiring(item.get("expires_at"), GOOGLE_REFRESH_SKEW_SECONDS):
            return refresh_google_token()
    if provider_id == "copilot":
        return resolve_copilot_api_token()
    return get_token(provider_id)


def env_or_token(provider_id: str, env_name: str = "") -> str:
    return resolve_provider_token(provider_id, env_name)


def auth_path() -> Path:
    return _auth_file()


def qwen_cli_auth_path() -> Path:
    home = os.environ.get("HOME")
    root = Path(home).expanduser() if home else Path.home()
    return root / ".qwen" / "oauth_creds.json"


def import_qwen_cli_tokens(path: Path | None = None) -> Path:
    src = path or qwen_cli_auth_path()
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OAuthAuthError(f"Qwen CLI credentials not found: {src}") from exc
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OAuthAuthError(f"Qwen CLI credentials cannot be read: {src}") from exc
    if not isinstance(raw, dict):
        raise OAuthAuthError(f"Qwen CLI credentials are invalid: {src}")
    access_token = str(raw.get("access_token") or "").strip()
    refresh_token = str(raw.get("refresh_token") or "").strip()
    if not access_token:
        raise OAuthAuthError("Qwen CLI credentials missing access_token")
    expiry_ms = raw.get("expiry_date") or raw.get("expires_at") or 0
    try:
        expiry_num = float(expiry_ms or 0)
    except (TypeError, ValueError):
        expiry_num = 0
    expires_at = int(expiry_num / 1000) if expiry_num > 10_000_000_000 else int(expiry_num)
    set_auth_token("qwen-oauth", access_token, refresh_token=refresh_token,
                   expires_at=expires_at, source=f"qwen-cli:{src}")
    return src


def qwen_cli_login(timeout: float | None = None) -> Path:
    qwen = shutil.which("qwen")
    if not qwen:
        raise OAuthAuthError("Qwen CLI not found. Install/login with Qwen CLI, or use --token / --import-qwen-cli.")
    try:
        result = subprocess.run([qwen, "auth", "qwen-oauth"], check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OAuthAuthError(f"Qwen CLI login failed: {exc}") from exc
    if result.returncode != 0:
        raise OAuthAuthError(f"Qwen CLI login failed with exit code {result.returncode}.")
    return import_qwen_cli_tokens()
