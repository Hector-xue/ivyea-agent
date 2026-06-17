"""领星 OpenAPI 适配（agent 自有，独立实现，不依赖 ivyea-ops）。

权威契约参照 ivyea-ops `lingxing_openapi.py`，2026-06-17 已用独立探针实测验证：
token + 签名 + 店铺列表 + SP 搜索词报表全部 code=0 通。

* 令牌：POST /api/auth-server/oauth/access-token?appId=&appSecret= → data.access_token，
  缓存到 ~/.ivyea/lingxing_token.json（带 expire_at，提前 120s 刷新，失败回退重取）。
* 签名：ksort(params) → 拼 k=v&（空串跳过、dict/list 紧凑 JSON 递归丢 None、bool→true/false、
  None→null）→ MD5().hexdigest().upper() → AES-128-ECB(key=appId, PKCS7) → base64。
* 组装：共用参数 access_token/timestamp(秒)/app_key=appId；GET 全参入 query，
  POST common 入 query + body 为紧凑 JSON 全参；成功 code ∈ {200,0,1}。
* 限流：全局 ≥1 次/秒（领星硬限）。

凭据只在本机：host/appid 存 settings.json，secret 存 ~/.ivyea/.env（LINGXING_OPENAPI_SECRET）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any, Optional

import httpx
from cryptography.hazmat.primitives import padding as _pad
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import config

_TOKEN_PATH_GET = "/api/auth-server/oauth/access-token"
_TOKEN_PATH_REFRESH = "/api/auth-server/oauth/refresh"
_REQUEST_TIMEOUT_S = 60.0
_TOKEN_SKEW_S = 120          # 提前这么多秒刷新
_RATE_MIN_INTERVAL_S = 0.34  # ≥1/s，留安全余量

_TOKEN_FILE = config.IVYEA_DIR / "lingxing_token.json"

# 全局限流（进程内）
_rate_lock = threading.Lock()
_last_call_ts = 0.0
# 令牌串行化
_token_lock = threading.Lock()


class LingXingError(RuntimeError):
    """领星 OpenAPI 任意失败（配置/传输/协议/业务）。"""


# ── 配置读取 ────────────────────────────────────────────────────────────────
def _host() -> str:
    return (config.get_setting("lingxing_openapi_host", "https://openapi.lingxing.com") or "").strip().rstrip("/")


def _appid() -> str:
    return (config.get_setting("lingxing_openapi_appid", "") or "").strip()


def _secret() -> str:
    config.load_env()
    import os
    return (os.environ.get("LINGXING_OPENAPI_SECRET", "") or "").strip()


def is_configured() -> bool:
    return bool(_host() and _appid() and _secret())


# ── 签名 ────────────────────────────────────────────────────────────────────
def _filter_array(v: Any) -> Any:
    """递归丢 None（对齐领星 SignService::filter_array）。"""
    if isinstance(v, dict):
        return {k: _filter_array(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_filter_array(x) for x in v if x is not None]
    return v


def _php_json(v: Any) -> str:
    """json_encode(JSON_UNESCAPED_UNICODE|JSON_UNESCAPED_SLASHES)：紧凑、无空格、不转义。"""
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def make_sign(params: dict[str, Any], app_id: str) -> str:
    parts: list[str] = []
    for k in sorted(params.keys()):
        v = params[k]
        if isinstance(v, (dict, list)):
            parts.append(f"{k}=" + _php_json(_filter_array(v)))
        elif v == "" and not isinstance(v, bool):
            continue  # 空串跳过（注意 bool False == '' 为假，不会误跳）
        else:
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif v is None:
                v = "null"
            parts.append(f"{k}={v}")
    canonical = "&".join(parts)
    md5_upper = hashlib.md5(canonical.encode("utf-8")).hexdigest().upper()
    padder = _pad.PKCS7(128).padder()
    data = padder.update(md5_upper.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(app_id.encode("utf-8")), modes.ECB()).encryptor()
    ct = enc.update(data) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


# ── 令牌生命周期 ──────────────────────────────────────────────────────────────
def _load_token() -> dict[str, Any]:
    try:
        return json.loads(_TOKEN_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _save_token(tok: dict[str, Any]) -> None:
    config.ensure_dirs()
    tmp = _TOKEN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tok, ensure_ascii=False), "utf-8")
    tmp.replace(_TOKEN_FILE)
    try:
        _TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def _unwrap_token(r: httpx.Response) -> dict[str, Any]:
    if r.status_code >= 400:
        raise LingXingError(f"令牌接口 HTTP {r.status_code}: {r.text[:200]}")
    try:
        body = r.json()
    except Exception as e:
        raise LingXingError(f"令牌响应非 JSON: {r.text[:200]}") from e
    if str(body.get("code")) not in ("200", "0"):
        raise LingXingError(f"令牌接口错误 {body.get('code')}: {body.get('msg') or body.get('message')}")
    data = body.get("data") or {}
    at = data.get("access_token")
    if not at:
        raise LingXingError("令牌响应缺少 access_token")
    return {"access_token": at, "refresh_token": data.get("refresh_token", ""),
            "expire_at": time.time() + float(data.get("expires_in") or 0)}


def _fetch_token(client: httpx.Client) -> dict[str, Any]:
    r = client.post(f"{_host()}{_TOKEN_PATH_GET}", params={"appId": _appid(), "appSecret": _secret()})
    return _unwrap_token(r)


def _refresh_token(client: httpx.Client, refresh_token: str) -> dict[str, Any]:
    r = client.post(f"{_host()}{_TOKEN_PATH_REFRESH}", params={"appId": _appid(), "refreshToken": refresh_token})
    return _unwrap_token(r)


def _ensure_token(client: httpx.Client) -> str:
    if not is_configured():
        raise LingXingError("未配置领星 OpenAPI 凭证（ivyea lingxing setup 或 ~/.ivyea/）")
    with _token_lock:
        tok = _load_token()
        if tok.get("access_token") and float(tok.get("expire_at", 0)) - _TOKEN_SKEW_S > time.time():
            return tok["access_token"]
        new: Optional[dict[str, Any]] = None
        if tok.get("refresh_token"):
            try:
                new = _refresh_token(client, tok["refresh_token"])
            except LingXingError:
                new = None
        if new is None:
            new = _fetch_token(client)
        _save_token(new)
        return new["access_token"]


# ── 限流 ────────────────────────────────────────────────────────────────────
def _throttle() -> None:
    global _last_call_ts
    with _rate_lock:
        wait = _RATE_MIN_INTERVAL_S - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


# ── 业务调用 ──────────────────────────────────────────────────────────────────
def call(route: str, params: Optional[dict[str, Any]] = None, *, method: str = "POST") -> dict[str, Any]:
    """已签名的领星 OpenAPI 调用。route 为业务路径，如 /erp/sc/data/seller/lists。"""
    params = dict(params or {})
    method = method.upper()
    with httpx.Client(timeout=_REQUEST_TIMEOUT_S, verify=True) as client:
        access_token = _ensure_token(client)
        common = {"access_token": access_token, "timestamp": int(time.time()), "app_key": _appid()}
        full = {**params, **common}
        sign = make_sign(full, _appid())
        url = f"{_host()}/{route.strip('/')}"
        headers = {"Content-Type": "application/json"}
        _throttle()
        try:
            if method == "GET":
                r = client.get(url, params={**full, "sign": sign}, headers=headers)
            else:
                r = client.request(method, url, params={**common, "sign": sign},
                                   content=_php_json(full), headers=headers)
        except httpx.HTTPError as e:
            raise LingXingError(f"领星 OpenAPI 连接失败: {e}") from e
    if r.status_code >= 400:
        raise LingXingError(f"领星 OpenAPI HTTP {r.status_code}: {r.text[:300]}")
    try:
        body = r.json()
    except Exception as e:
        raise LingXingError(f"领星 OpenAPI 响应非 JSON: {r.text[:300]}") from e
    code = str(body.get("code"))
    if code not in ("200", "0", "1") and body.get("success") is not True:
        raise LingXingError(f"领星 OpenAPI 业务错误 code={code} msg={body.get('message') or body.get('msg')}")
    return body


def verify() -> dict[str, Any]:
    """端到端凭据+签名自检：取令牌 + 拉店铺列表（无副作用）。"""
    if not is_configured():
        raise LingXingError("未配置领星 OpenAPI 凭证")
    res = call("/erp/sc/data/seller/lists", {}, method="GET")
    data = res.get("data")
    n = len(data) if isinstance(data, list) else None
    return {"ok": True, "probe_route": "/erp/sc/data/seller/lists",
            "probe_code": str(res.get("code")), "probe_seller_count": n,
            "probe_msg": res.get("message") or res.get("msg") or ""}
