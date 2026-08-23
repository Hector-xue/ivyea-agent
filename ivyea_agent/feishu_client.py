"""飞书应用身份客户端 —— 发卡片、改卡片、发文本。

为什么不走 lark-mcp：告警是关键路径，不该依赖一个 npx 子进程冷启动（实测首次
要几十秒）。而且卡片原地更新需要拿到 ``message_id``，MCP 那层的返回不保证透传。
lark-mcp 留给 agent 做多维表格/文档这类交互式工作，告警走这里直连。

token 管理照抄 ``lingxing_openapi`` 的成熟做法：落盘缓存、提前刷新、失败重取。

**不在这里做脱敏**——卡片在 ``feishu_card`` 构建时已递归脱敏。这里只管传输。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

import httpx

from . import config

_TOKEN_FILE = config.IVYEA_DIR / "feishu_token.json"
_LOCK = threading.Lock()

#: 提前 5 分钟刷新，避免边界上用到刚过期的 token
_REFRESH_MARGIN = 300.0

_DOMAINS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}


class FeishuError(Exception):
    """飞书 API 返回非 0 code，或网络失败。"""

    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


def _domain() -> str:
    config.load_env()
    name = str(config.load_settings().get("feishu_domain") or "feishu").lower()
    return _DOMAINS.get(name, _DOMAINS["feishu"])


def _creds() -> tuple[str, str]:
    import os
    config.load_env()
    settings = config.load_settings()
    app_id = os.environ.get("IVYEA_FEISHU_APP_ID") or settings.get("feishu_app_id", "")
    secret = os.environ.get("IVYEA_FEISHU_APP_SECRET") or settings.get("feishu_app_secret", "")
    return str(app_id or ""), str(secret or "")


def is_configured() -> bool:
    app_id, secret = _creds()
    return bool(app_id and secret)


def default_chat_id() -> str:
    config.load_env()
    return str(config.load_settings().get("feishu_default_chat_id") or "")


# ── token ───────────────────────────────────────────────────────────────────
def _load_token() -> dict[str, Any]:
    if not _TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_token(tok: dict[str, Any]) -> None:
    import os
    config.ensure_dirs()
    _TOKEN_FILE.write_text(json.dumps(tok, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(_TOKEN_FILE, 0o600)
    except OSError:
        pass


def _fetch_token(client: httpx.Client) -> dict[str, Any]:
    app_id, secret = _creds()
    if not app_id or not secret:
        raise FeishuError("未配置飞书应用凭据（IVYEA_FEISHU_APP_ID / IVYEA_FEISHU_APP_SECRET）")
    r = client.post(f"{_domain()}/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": secret})
    try:
        data = r.json()
    except ValueError as exc:
        raise FeishuError(f"token 响应不可解析：{r.status_code}") from exc
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise FeishuError(f"取 token 失败：{data.get('msg')}", data.get("code"))
    return {"token": data["tenant_access_token"],
            "expires_at": time.time() + float(data.get("expire") or 7200)}


def access_token(*, force: bool = False) -> str:
    with _LOCK:
        tok = _load_token()
        if (not force and tok.get("token")
                and float(tok.get("expires_at") or 0) - _REFRESH_MARGIN > time.time()):
            return str(tok["token"])
        with httpx.Client(timeout=20.0) as client:
            tok = _fetch_token(client)
        _save_token(tok)
        return str(tok["token"])


# ── 请求 ────────────────────────────────────────────────────────────────────
def _call(method: str, path: str, *, params: Optional[dict] = None,
          body: Optional[dict] = None, timeout: float = 20.0,
          _retried: bool = False) -> dict[str, Any]:
    token = access_token()
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json; charset=utf-8"}
    url = f"{_domain()}{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.request(method, url, headers=headers, params=params, json=body)
    except httpx.HTTPError as exc:
        raise FeishuError(f"飞书请求失败：{exc}") from exc
    try:
        data = r.json()
    except ValueError as exc:
        raise FeishuError(f"飞书响应不可解析（HTTP {r.status_code}）") from exc
    code = data.get("code")
    if code in (99991663, 99991661, 99991664) and not _retried:
        # token 失效：强制刷新后重试一次。缓存过期时间只是"声称"，以服务端为准。
        access_token(force=True)
        return _call(method, path, params=params, body=body, timeout=timeout, _retried=True)
    if code != 0:
        raise FeishuError(f"飞书接口错误 code={code} msg={data.get('msg')}", code)
    return data.get("data") or {}


# ── 消息 ────────────────────────────────────────────────────────────────────
def send_card(chat_id: str, card: dict[str, Any]) -> str:
    """发一张交互卡片，返回 message_id（后续原地更新要用）。"""
    chat_id = chat_id or default_chat_id()
    if not chat_id:
        raise FeishuError("未指定 chat_id，且未配置 feishu_default_chat_id")
    data = _call("POST", "/open-apis/im/v1/messages",
                 params={"receive_id_type": "chat_id"},
                 body={"receive_id": chat_id, "msg_type": "interactive",
                       "content": json.dumps(card, ensure_ascii=False)})
    return str(data.get("message_id") or "")


def send_text(chat_id: str, text: str) -> str:
    chat_id = chat_id or default_chat_id()
    if not chat_id:
        raise FeishuError("未指定 chat_id，且未配置 feishu_default_chat_id")
    data = _call("POST", "/open-apis/im/v1/messages",
                 params={"receive_id_type": "chat_id"},
                 body={"receive_id": chat_id, "msg_type": "text",
                       "content": json.dumps({"text": text}, ensure_ascii=False)})
    return str(data.get("message_id") or "")


def update_card(message_id: str, card: dict[str, Any]) -> bool:
    """原地替换已发出的卡片（点完按钮防重复点击要用）。"""
    if not message_id:
        raise FeishuError("update_card 需要 message_id")
    _call("PATCH", f"/open-apis/im/v1/messages/{message_id}",
          body={"content": json.dumps(card, ensure_ascii=False)})
    return True


def reply_card(message_id: str, card: dict[str, Any]) -> str:
    """在原消息下回复一张卡片（执行结果挂在告警下面，不另起一条）。"""
    data = _call("POST", f"/open-apis/im/v1/messages/{message_id}/reply",
                 body={"msg_type": "interactive",
                       "content": json.dumps(card, ensure_ascii=False)})
    return str(data.get("message_id") or "")


def verify() -> dict[str, Any]:
    """连通性自检：取 token + 查机器人所在会话数。"""
    if not is_configured():
        return {"ok": False, "error": "未配置飞书应用凭据"}
    try:
        access_token(force=True)
        data = _call("GET", "/open-apis/im/v1/chats", params={"page_size": 20})
    except FeishuError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code}
    items = data.get("items") or []
    return {"ok": True, "chat_count": len(items),
            "default_chat_id": default_chat_id(),
            "chats": [{"chat_id": c.get("chat_id"), "name": c.get("name")} for c in items]}


def list_chats(page_size: int = 50) -> list[dict[str, Any]]:
    """机器人所在的会话清单。

    配置向导要用：chat_id 形如 ``oc_8f...``，让人去飞书里翻出来手抄是配置流程里
    最容易抄错的一步（抄错的表现是"保存成功但一条消息都收不到"，还没有报错）。
    能列出来就让他点。
    """
    data = _call("GET", "/open-apis/im/v1/chats", params={"page_size": max(1, min(100, page_size))})
    return [{"chat_id": c.get("chat_id"), "name": c.get("name") or "",
             "description": c.get("description") or ""}
            for c in (data.get("items") or [])]


def list_chat_members(chat_id: str, page_size: int = 100) -> list[dict[str, Any]]:
    """群成员（open_id + 名字）。审批白名单要按人选，同样不该让人手抄 ou_xxx。

    要求应用有 ``im:chat:readonly``（或 contact 相关）权限；没有权限时飞书返回
    非 0 code，由调用方转成"这一步还差个权限"的提示，而不是静默给空列表——
    空列表会被误读成"这个群没人"。
    """
    chat_id = chat_id or default_chat_id()
    if not chat_id:
        raise FeishuError("未指定 chat_id，且未配置 feishu_default_chat_id")
    data = _call("GET", f"/open-apis/im/v1/chats/{chat_id}/members",
                 params={"member_id_type": "open_id",
                         "page_size": max(1, min(100, page_size))})
    return [{"open_id": m.get("member_id"), "name": m.get("name") or ""}
            for m in (data.get("items") or [])]
