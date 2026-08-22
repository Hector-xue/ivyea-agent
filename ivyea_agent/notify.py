"""Notification adapters for local automation output."""
from __future__ import annotations

import os
from typing import Any

import httpx

from . import config
from .security import redact_text

#: ``feishu`` = 群自定义机器人 webhook（纯文本，无按钮，零配置成本）
#: ``feishu_app`` = 应用身份（交互卡片 + 可原地更新 + 拿得到 message_id）
ALLOWED_CHANNELS = {"stdout", "webhook", "feishu", "feishu_app"}


def _configured_webhook_url(channel: str, override: str = "") -> str:
    if override:
        return override
    config.load_env()
    settings = config.load_settings()
    if channel == "feishu":
        return (
            os.environ.get("IVYEA_FEISHU_WEBHOOK_URL")
            or settings.get("feishu_webhook_url", "")
            or settings.get("notify_webhook_url", "")
        )
    return os.environ.get("IVYEA_NOTIFY_WEBHOOK_URL") or settings.get("notify_webhook_url", "")


def build_payload(title: str, message: str, *, channel: str = "webhook") -> dict[str, Any]:
    title = redact_text(title or "Ivyea Agent")
    message = redact_text(message)
    if channel == "feishu":
        return {"msg_type": "text", "content": {"text": f"{title}\n\n{message}"}}
    return {"title": title, "text": message, "source": "ivyea-agent"}


def send_card(card: dict[str, Any], *, chat_id: str = "",
              extra_parts: Any = (), title: str = "Ivyea Agent") -> dict[str, Any]:
    """经应用身份发一张交互卡片。返回 {ok, message_id, chat_id}。

    卡片内容在 ``feishu_card`` 构建时已递归脱敏，这里不再重复处理。
    """
    from . import feishu_card, feishu_client

    if not feishu_client.is_configured():
        return {"ok": False, "channel": "feishu_app",
                "error": "未配置飞书应用凭据（IVYEA_FEISHU_APP_ID / IVYEA_FEISHU_APP_SECRET）"}
    target = chat_id or feishu_client.default_chat_id()
    try:
        message_id = feishu_client.send_card(target, card)
        for part in extra_parts or ():
            feishu_client.send_card(target, feishu_card.build_text_card(f"{title}（续）", part))
    except feishu_client.FeishuError as exc:
        return {"ok": False, "channel": "feishu_app", "error": redact_text(str(exc))}
    return {"ok": True, "channel": "feishu_app", "message_id": message_id, "chat_id": target}


def send_alert(text: str, *, card: dict[str, Any] | None = None,
               chat_id: str = "", title: str = "Ivyea 告警") -> dict[str, Any]:
    """带降级链的告警发送（方案 §8.1）。

    顺序：应用身份卡片 → 群机器人 webhook 纯文本 → 显式失败。
    长连接/应用侧出问题时，告警**不能就这么没了**；webhook 是独立通道，
    不依赖应用凭据与长连接，正适合当兜底。

    返回里带 ``degraded``：走了兜底就标出来，调用方要能在日志里看见。
    """
    attempts: list[dict[str, Any]] = []
    if card is not None:
        primary = send_card(card, chat_id=chat_id, title=title)
    else:
        primary = send(text, title=title, channel="feishu_app", chat_id=chat_id)
    attempts.append({"channel": "feishu_app", "ok": bool(primary.get("ok")),
                     "error": primary.get("error", "")})
    if primary.get("ok"):
        return dict(primary, degraded=False, attempts=attempts)

    fallback_url = _configured_webhook_url("feishu")
    if fallback_url:
        second = send(text, title=title, channel="feishu")
        attempts.append({"channel": "feishu", "ok": bool(second.get("ok")),
                         "error": second.get("error", "")})
        if second.get("ok"):
            return dict(second, degraded=True, attempts=attempts,
                        degraded_from="feishu_app")
    else:
        attempts.append({"channel": "feishu", "ok": False,
                         "error": "未配置群机器人 webhook，无兜底通道"})

    return {"ok": False, "channel": "none", "degraded": True, "attempts": attempts,
            "error": "所有通道均失败：" + "；".join(
                f"{a['channel']}={a['error']}" for a in attempts if a.get("error"))}


def send(
    message: str,
    *,
    title: str = "Ivyea Agent",
    channel: str = "stdout",
    webhook_url: str = "",
    chat_id: str = "",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Send a notification.

    ``stdout`` is intentionally supported as a first-class channel so cron,
    systemd timers, and tests can exercise the same path without network access.
    """
    channel = (channel or "stdout").strip().lower()
    if channel not in ALLOWED_CHANNELS:
        raise ValueError(f"未知通知通道：{channel}，可用：{', '.join(sorted(ALLOWED_CHANNELS))}")
    safe_title = redact_text(title or "Ivyea Agent")
    safe_message = redact_text(message)
    if channel == "stdout":
        return {"ok": True, "channel": channel, "title": safe_title, "message": safe_message}

    if channel == "feishu_app":
        from . import feishu_card
        # 长文本切片后逐条发；只有首条的 message_id 会被返回给调用方
        parts = feishu_card.chunk(safe_message) or [""]
        return send_card(feishu_card.build_text_card(safe_title, parts[0]),
                         chat_id=chat_id, extra_parts=parts[1:], title=safe_title)

    url = _configured_webhook_url(channel, webhook_url)
    if not url:
        return {
            "ok": False,
            "channel": channel,
            "error": "未配置 webhook URL。可传 --webhook-url，或设置 IVYEA_NOTIFY_WEBHOOK_URL / IVYEA_FEISHU_WEBHOOK_URL。",
        }

    payload = build_payload(safe_title, safe_message, channel=channel)
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return {"ok": False, "channel": channel, "error": redact_text(str(exc))}
    return {"ok": True, "channel": channel, "status_code": resp.status_code}


def render_result(result: dict[str, Any]) -> str:
    if result.get("ok"):
        channel = result.get("channel", "stdout")
        if channel == "stdout":
            return f"{result.get('title', 'Ivyea Agent')}\n\n{result.get('message', '')}\n"
        if channel == "feishu_app":
            return f"卡片已发送：message_id={result.get('message_id', '-')}"
        return f"通知发送成功：channel={channel} status={result.get('status_code', '-')}"
    return f"通知发送失败：channel={result.get('channel', '-')} error={result.get('error', '-')}"
