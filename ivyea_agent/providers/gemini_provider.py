"""Google Gemini native provider.

Gemini's native REST API is not OpenAI-compatible: messages are `contents`,
system text is `systemInstruction`, tool calls are `functionCall` parts, and
tool results are `functionResponse` parts.  This adapter keeps the rest of
Ivyea on the existing OpenAI-shaped provider interface.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx

from .base import LLMError, LLMProvider

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MAX_OUTPUT_TOKENS = 8192
_RETRIES = 3
_RETRYABLE = {429, 500, 502, 503, 504}


def _backoff(attempt: int) -> float:
    return min(8.0, 0.8 * (2 ** attempt))


def _schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep Gemini function schemas small and JSON-schema compatible."""
    allowed = {"type", "properties", "required", "description", "enum", "items", "nullable"}
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in allowed:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {str(k): _schema_for_gemini(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _schema_for_gemini(value)
        else:
            out[key] = value
    return out or {"type": "object", "properties": {}}


def _tools_to_gemini(tools: Optional[list]) -> Optional[list[dict[str, Any]]]:
    if not tools:
        return None
    declarations = []
    for tool in tools:
        fn = tool.get("function", tool)
        declarations.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": _schema_for_gemini(fn.get("parameters") or {"type": "object", "properties": {}}),
        })
    return [{"functionDeclarations": declarations}] if declarations else None


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _messages_to_gemini(messages: list[dict]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    tool_name_by_id: dict[str, str] = {}

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            text = _content_text(msg.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role == "assistant":
            parts: list[dict[str, Any]] = []
            text = _content_text(msg.get("content"))
            if text:
                parts.append({"text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_name_by_id[str(tc.get("id") or "")] = name
                parts.append({"functionCall": {"name": name, "args": args if isinstance(args, dict) else {}}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            continue

        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            name = tool_name_by_id.get(call_id) or "tool_result"
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": name, "response": {"result": _content_text(msg.get("content"))}}}],
            })
            continue

        content = msg.get("content")
        parts_u: list[dict[str, Any]] = []
        text = _content_text(content)
        if text:
            parts_u.append({"text": text})
        if isinstance(content, list):                     # 多模态→Gemini inlineData
            for it in content:
                if isinstance(it, dict) and it.get("type") == "image_url":
                    iu = it.get("image_url")
                    url = iu.get("url") if isinstance(iu, dict) else iu
                    if url and str(url).startswith("data:"):
                        head, b64 = str(url).split(",", 1)
                        mime = head.split(":", 1)[1].split(";", 1)[0]
                        parts_u.append({"inlineData": {"mimeType": mime, "data": b64}})
        contents.append({"role": "user", "parts": parts_u or [{"text": ""}]})

    return "\n\n".join(system_parts), contents


def _extract_response(data: dict[str, Any]) -> dict[str, Any]:
    candidate = (data.get("candidates") or [{}])[0]
    content = candidate.get("content") or {}
    parts = content.get("parts") or []
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for idx, part in enumerate(parts):
        if part.get("text"):
            texts.append(str(part["text"]))
        fc = part.get("functionCall")
        if isinstance(fc, dict):
            tool_calls.append({
                "id": f"gemini_call_{idx}",
                "name": str(fc.get("name") or ""),
                "arguments": fc.get("args") if isinstance(fc.get("args"), dict) else {},
            })
    usage = data.get("usageMetadata") or {}
    return {
        "role": "assistant",
        "content": "".join(texts),
        "tool_calls": tool_calls,
        "usage": {
            "prompt_tokens": int(usage.get("promptTokenCount") or 0),
            "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
        },
    }


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        super().__init__(api_key, model or "gemini-3-pro-preview")
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def _post(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY 未配置（ivyea model 选 Gemini 并配 key）")
        url = f"{self.base_url}/models/{self.model}:generateContent"
        last = ""
        for attempt in range(_RETRIES):
            try:
                resp = httpx.post(url, params={"key": self.api_key}, json=payload,
                                  headers={"Content-Type": "application/json"}, timeout=timeout)
            except httpx.HTTPError as exc:
                last = f"Gemini 连接失败：{exc}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                last = f"Gemini HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in _RETRYABLE:
                    raise LLMError(last)
            if attempt < _RETRIES - 1:
                time.sleep(_backoff(attempt))
        raise LLMError(f"Gemini 已重试 {_RETRIES} 次仍失败：{last}")

    def complete(self, system: str, user: str, json_mode: bool = False,
                 temperature: float = 0.2, timeout: float = 60.0) -> str:
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": DEFAULT_MAX_OUTPUT_TOKENS,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = self._post(payload, timeout)
        return _extract_response(data)["content"]

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        system, contents = _messages_to_gemini(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": DEFAULT_MAX_OUTPUT_TOKENS,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        gtools = _tools_to_gemini(tools)
        if gtools:
            payload["tools"] = gtools
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        return _extract_response(self._post(payload, timeout))
