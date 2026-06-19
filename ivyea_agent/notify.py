"""Notification adapters for local automation output."""
from __future__ import annotations

import os
from typing import Any

import httpx

from . import config
from .security import redact_text

ALLOWED_CHANNELS = {"stdout", "webhook", "feishu"}


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


def send(
    message: str,
    *,
    title: str = "Ivyea Agent",
    channel: str = "stdout",
    webhook_url: str = "",
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
        return f"通知发送成功：channel={channel} status={result.get('status_code', '-')}"
    return f"通知发送失败：channel={result.get('channel', '-')} error={result.get('error', '-')}"
