"""Provider-neutral multimodal vision request packages."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from . import image_audit
from .security import redact_obj


DEFAULT_MODELS = {
    "openai": "gpt-5-mini",
    "anthropic": "claude-sonnet-4-6",
    "gemini": "gemini-2.5-flash",
}

API_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _image_paths(result: dict[str, Any], limit: int = 8) -> list[str]:
    return [i["path"] for i in result.get("images", [])[:limit]]


def build(provider: str, paths: list[str], *, product_context: str = "",
          model: str = "", max_images: int = 8) -> dict[str, Any]:
    """Build a dry-run multimodal request package.

    The package is intentionally serializable and does not call external APIs.
    """
    provider = (provider or "openai").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError("provider 仅支持 openai / anthropic / gemini")
    result = image_audit.audit(paths)
    prompt = image_audit.multimodal_prompt(result, product_context=product_context)
    images = _image_paths(result, limit=max_images)
    model = model or DEFAULT_MODELS[provider]
    payload = _payload(provider, model, prompt, images)
    return {
        "provider": provider,
        "model": model,
        "images": images,
        "prompt": prompt,
        "local_audit": result,
        "payload": payload,
    }


def build_general(provider: str, paths: list[str], *, task: str = "", context: str = "",
                  model: str = "", max_images: int = 8) -> dict[str, Any]:
    """Build a generic screenshot/report/UI multimodal request package."""
    provider = (provider or "openai").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError("provider 仅支持 openai / anthropic / gemini")
    result = image_audit.audit(paths)
    images = _image_paths(result, limit=max_images)
    model = model or DEFAULT_MODELS[provider]
    prompt = generic_prompt(result, task=task, context=context)
    payload = _payload(provider, model, prompt, images)
    return {
        "provider": provider,
        "model": model,
        "images": images,
        "prompt": prompt,
        "local_audit": result,
        "payload": payload,
        "mode": "general",
    }


def generic_prompt(result: dict[str, Any], *, task: str = "", context: str = "") -> str:
    images = result.get("images", [])
    lines = [
        "你是 Ivyea Agent 的通用多模态视觉分析器。",
        "",
        "请基于图片内容完成用户任务，优先输出可执行结论，不要编造图片中不存在的信息。",
        "",
        "## 用户任务",
        task or "识别图片/截图/报表中的关键信息、异常、风险和下一步建议。",
    ]
    if context:
        lines.extend(["", "## 上下文", context])
    lines.extend(["", "## 本地图片预检查"])
    if not images:
        lines.append("- 未发现可用图片。")
    for img in images[:12]:
        lines.append(
            f"- {img.get('path')} size={img.get('width')}x{img.get('height')} "
            f"bytes={img.get('bytes')} role={img.get('role')}"
        )
    lines.extend([
        "",
        "## 输出格式",
        "1. 先给结论摘要。",
        "2. 列出图片中直接可见的证据。",
        "3. 标出不确定点和需要人工确认的内容。",
        "4. 给出下一步动作清单。",
    ])
    return "\n".join(lines)


def _payload(provider: str, model: str, prompt: str, images: list[str]) -> dict[str, Any]:
    if provider == "openai":
        return {
            "model": model,
            "input": [{
                "role": "user",
                "content": (
                    [{"type": "input_text", "text": prompt}] +
                    [{"type": "input_image", "image_url": image_audit.data_url(p)} for p in images]
                ),
            }],
        }
    if provider == "anthropic":
        content = [{"type": "text", "text": prompt}]
        for p in images:
            data_url = image_audit.data_url(p)
            mime, b64 = data_url.split(",", 1)
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime.split(":", 1)[1].split(";", 1)[0],
                    "data": b64,
                },
            })
        return {"model": model, "max_tokens": 3000, "messages": [{"role": "user", "content": content}]}
    if provider == "gemini":
        parts = [{"text": prompt}]
        for p in images:
            data_url = image_audit.data_url(p)
            mime, b64 = data_url.split(",", 1)
            parts.append({"inline_data": {
                "mime_type": mime.split(":", 1)[1].split(";", 1)[0],
                "data": b64,
            }})
        return {"model": model, "contents": [{"role": "user", "parts": parts}]}
    raise ValueError(f"未知 provider: {provider}")


def render_package(pkg: dict[str, Any], *, include_payload: bool = False) -> str:
    lines = [
        "# 多模态视觉请求包",
        "",
        f"- provider: {pkg['provider']}",
        f"- model: {pkg['model']}",
        f"- images: {len(pkg['images'])}",
        "",
        "## 图片",
    ]
    if not pkg["images"]:
        lines.append("（无图片，无法进行真实视觉审核）")
    else:
        for p in pkg["images"]:
            lines.append(f"- {p}")
    lines.extend(["", "## Prompt", "", pkg["prompt"]])
    if include_payload:
        redacted = json.loads(json.dumps(pkg["payload"], ensure_ascii=False))
        _truncate_images(redacted)
        lines.extend(["", "## Payload Preview", "", "```json", json.dumps(redacted, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines) + "\n"


def _truncate_images(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in ("image_url", "data") and isinstance(v, str) and len(v) > 120:
                obj[k] = v[:80] + "...<base64 truncated>"
            else:
                _truncate_images(v)
    elif isinstance(obj, list):
        for item in obj:
            _truncate_images(item)


def write_package(pkg: dict[str, Any], path: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    safe = json.loads(json.dumps(pkg, ensure_ascii=False))
    _truncate_images(safe)
    p.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def _api_key(provider: str, override: str = "") -> str:
    if override:
        return override
    env = API_ENV_KEYS.get(provider, "")
    return os.environ.get(env, "") if env else ""


def _endpoint(provider: str, model: str) -> str:
    if provider == "openai":
        return "https://api.openai.com/v1/responses"
    if provider == "anthropic":
        return "https://api.anthropic.com/v1/messages"
    if provider == "gemini":
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    raise ValueError(f"未知 provider: {provider}")


def _headers(provider: str, api_key: str) -> dict[str, str]:
    if provider == "openai":
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if provider == "anthropic":
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    if provider == "gemini":
        return {"Content-Type": "application/json"}
    raise ValueError(f"未知 provider: {provider}")


def _extract_text(provider: str, data: dict[str, Any]) -> str:
    if provider == "openai":
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        chunks: list[str] = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                text = content.get("text") or content.get("output_text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks).strip()
    if provider == "anthropic":
        chunks = [c.get("text", "") for c in data.get("content", []) or [] if c.get("type") == "text"]
        return "\n".join(chunks).strip()
    if provider == "gemini":
        chunks = []
        for cand in data.get("candidates", []) or []:
            for part in (cand.get("content") or {}).get("parts", []) or []:
                if isinstance(part.get("text"), str):
                    chunks.append(part["text"])
        return "\n".join(chunks).strip()
    return ""


def call(
    pkg: dict[str, Any],
    *,
    api_key: str = "",
    timeout: float = 120.0,
) -> dict[str, Any]:
    provider = pkg["provider"]
    key = _api_key(provider, api_key)
    if not key:
        env = API_ENV_KEYS.get(provider, "")
        return {"ok": False, "provider": provider, "error": f"缺少 API key：请设置 {env} 或传 --api-key。"}
    url = _endpoint(provider, pkg["model"])
    headers = _headers(provider, key)
    params = {"key": key} if provider == "gemini" else None
    try:
        resp = httpx.post(url, headers=headers, params=params, json=pkg["payload"], timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        return {"ok": False, "provider": provider, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "provider": provider, "error": f"响应不是 JSON：{exc}"}
    return {
        "ok": True,
        "provider": provider,
        "model": pkg["model"],
        "text": _extract_text(provider, data),
        "raw": redact_obj(data),
    }


def render_call(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"视觉模型调用失败：provider={result.get('provider', '-')} error={result.get('error', '-')}\n"
    text = result.get("text") or "（模型未返回可抽取文本，查看 raw 字段）"
    return (
        "# 多模态视觉审核结果\n\n"
        f"- provider: {result.get('provider')}\n"
        f"- model: {result.get('model')}\n\n"
        "## 结论\n\n"
        f"{text}\n"
    )
