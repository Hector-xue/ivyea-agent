"""OpenAI Codex OAuth Responses provider."""
from __future__ import annotations

import base64
import json
from typing import Any, Iterable, Iterator

import httpx

from .base import LLMError, LLMProvider

CODEX_MODEL_FALLBACKS = ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5-codex")


def _json_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _codex_account_id(access_token: str) -> str:
    parts = access_token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    auth = data.get("https://api.openai.com/auth")
    if isinstance(auth, dict) and isinstance(auth.get("chatgpt_account_id"), str):
        return auth["chatgpt_account_id"]
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _unsupported_model_error(exc: LLMError) -> bool:
    text = str(exc).lower()
    return "model is not supported" in text or "unsupported model" in text


def _model_candidates(model: str) -> list[str]:
    out: list[str] = []
    for item in [model, *CODEX_MODEL_FALLBACKS]:
        if item and item not in out:
            out.append(item)
    return out


def _normalize_usage(u: dict) -> dict:
    """Responses API 的 usage（input_tokens/output_tokens/input_tokens_details.cached_tokens）
    归一成内部契约的 chat-completions 形状（prompt_tokens/completion_tokens/
    prompt_cache_hit_tokens）——否则 agent_loop 的用量累计/stream-json 的 result
    统计读不到字段，全显示 0。已是 chat-completions 形状则原样返回。"""
    if not isinstance(u, dict) or not u:
        return {}
    if "prompt_tokens" in u or "completion_tokens" in u:
        return u
    cached = ((u.get("input_tokens_details") or {}).get("cached_tokens")
              or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    try:
        return {
            "prompt_tokens": int(u.get("input_tokens") or 0),
            "completion_tokens": int(u.get("output_tokens") or 0),
            "prompt_cache_hit_tokens": int(cached or 0),
            "prompt_tokens_details": {"cached_tokens": int(cached or 0)},
        }
    except (TypeError, ValueError):
        return {}


def parse_responses_sse(lines: Iterable[str]) -> Iterator[dict]:
    """Parse OpenAI Responses SSE events into Ivyea stream events."""
    content_parts: list[str] = []
    tools: dict[int, dict[str, str]] = {}
    usage: dict = {}

    def tool_slot(index: Any = 0) -> dict[str, str]:
        try:
            idx = int(index or 0)
        except (TypeError, ValueError):
            idx = 0
        return tools.setdefault(idx, {"id": "", "name": "", "arguments": ""})

    for raw in lines:
        if not raw:
            continue
        line = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        event_type = str(chunk.get("type") or "")
        if isinstance(chunk.get("response"), dict) and chunk["response"].get("usage"):
            usage = chunk["response"]["usage"]
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]

        if event_type in ("response.output_text.delta", "response.refusal.delta"):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                content_parts.append(delta)
                yield {"type": "text", "text": delta}
            continue

        if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                yield {"type": "reasoning", "text": delta}   # 思考流 → 思考事件
            continue

        if event_type == "response.function_call_arguments.delta":
            slot = tool_slot(chunk.get("output_index"))
            delta = chunk.get("delta")
            if isinstance(delta, str):
                slot["arguments"] += delta
            continue

        if event_type in ("response.output_item.added", "response.output_item.done"):
            item = chunk.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in ("function_call", "custom_tool_call"):
                slot = tool_slot(chunk.get("output_index"))
                slot["id"] = str(item.get("call_id") or item.get("id") or slot["id"])
                slot["name"] = str(item.get("name") or slot["name"])
                raw_args = item.get("arguments") if item_type == "function_call" else item.get("input")
                if isinstance(raw_args, str) and raw_args and not slot["arguments"]:
                    slot["arguments"] = raw_args
            elif item_type == "message":
                if content_parts:
                    continue
                normalized = _normalize_response({"output": [item]})
                text = normalized.get("content") or ""
                if text:
                    content_parts.append(text)
                    yield {"type": "text", "text": text}
            continue

        if event_type in ("response.completed", "response.done"):
            response = chunk.get("response")
            if isinstance(response, dict):
                normalized = _normalize_response(response)
                if not content_parts and normalized.get("content"):
                    text = normalized["content"]
                    content_parts.append(text)
                    yield {"type": "text", "text": text}
                if not tools:
                    for idx, call in enumerate(normalized.get("tool_calls") or []):
                        tools[idx] = {
                            "id": str(call.get("id") or ""),
                            "name": str(call.get("name") or ""),
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                        }
            break

    final_tools: list[dict[str, Any]] = []
    for _, slot in sorted(tools.items()):
        final_tools.append({
            "id": slot["id"],
            "name": slot["name"],
            "arguments": _json_args(slot["arguments"]),
        })
    yield {
        "type": "final",
        "content": "".join(content_parts),
        "tool_calls": final_tools,
        "usage": _normalize_usage(usage),
    }


def _normalize_response(data: dict) -> dict:
    texts: list[str] = []
    tool_calls: list[dict] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    text = part.get("text")
                    if isinstance(text, str):
                        texts.append(text)
        if item_type in ("function_call", "custom_tool_call"):
            raw_args = item.get("arguments") if item_type == "function_call" else item.get("input")
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "name": item.get("name") or "",
                "arguments": _json_args(raw_args),
            })
    if not texts and isinstance(data.get("output_text"), str):
        texts.append(data["output_text"])
    return {"role": "assistant", "content": "\n".join(t for t in texts if t), "tool_calls": tool_calls}


class CodexProvider(LLMProvider):
    name = "openai-codex"

    def __init__(self, api_key: str, model: str, base_url: str):
        super().__init__(api_key, model)
        self.base_url = (base_url or "https://chatgpt.com/backend-api/codex").rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "codex_cli_rs/0.0.0 (Ivyea Agent)",
            "originator": "codex_cli_rs",
        }
        account_id = _codex_account_id(self.api_key)
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        return headers

    def _tools(self, tools: list | None) -> list:
        out = []
        for tool in tools or []:
            fn = tool.get("function") if isinstance(tool, dict) else None
            if not isinstance(fn, dict):
                continue
            out.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        return out

    def _input(self, messages: list) -> tuple[str, list[dict[str, Any]]]:
        instructions = ""
        items: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "system":
                text = _content_text(msg.get("content"))
                instructions = f"{instructions}\n\n{text}".strip() if instructions and text else text or instructions
                continue
            if role == "tool":
                call_id = str(msg.get("tool_call_id") or msg.get("call_id") or "").strip()
                if call_id:
                    items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": _content_text(msg.get("content")),
                    })
                continue
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    continue
                call_id = str(call.get("id") or call.get("call_id") or "").strip()
                if call_id:
                    arguments = fn.get("arguments") or "{}"
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    items.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": fn.get("name", ""),
                        "arguments": arguments,
                    })
            content = msg.get("content")
            item_role = "assistant" if role == "assistant" else "user"
            parts: list[dict[str, Any]] = []
            if isinstance(content, list) and item_role == "user":   # 多模态：附带图片
                for it in content:
                    if isinstance(it, dict) and it.get("type") == "image_url":
                        iu = it.get("image_url")
                        url = iu.get("url") if isinstance(iu, dict) else iu
                        if url:
                            parts.append({"type": "input_image", "image_url": url})
            text = _content_text(content)
            if text:
                text_type = "output_text" if item_role == "assistant" else "input_text"
                parts.insert(0, {"type": text_type, "text": text})
            if parts:
                items.append({"role": item_role, "content": parts})
        return instructions, items

    def _post(self, payload: dict, timeout: float) -> dict:
        if not self.api_key:
            raise LLMError("OpenAI Codex OAuth token 未配置")
        try:
            resp = httpx.post(f"{self.base_url}/responses", headers=self._headers(),
                              json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise LLMError(f"Codex Responses 连接失败：{exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"Codex Responses HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"Codex Responses 返回非 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise LLMError("Codex Responses 返回格式异常")
        return data

    def _normalize(self, data: dict) -> dict:
        return _normalize_response(data)

    def complete(self, system: str, user: str, json_mode: bool = False,
                 temperature: float = 0.2, timeout: float = 60.0) -> str:
        msg = self.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ], tools=None, temperature=temperature, timeout=timeout)
        return msg.get("content") or ""

    def chat(self, messages: list, tools: list | None = None,
             temperature: float = 0.3, timeout: float = 120.0) -> dict:
        text_parts: list[str] = []
        final: dict[str, Any] | None = None
        for event in self.stream_chat(messages, tools=tools, temperature=temperature, timeout=timeout):
            if event.get("type") == "text":
                text = event.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif event.get("type") == "final":
                final = event
        content = str(final.get("content") or "") if final else ""
        if not content and text_parts:
            content = "".join(text_parts)
        return {
            "role": "assistant",
            "content": content,
            "tool_calls": (final or {}).get("tool_calls") or [],
            "usage": (final or {}).get("usage") or {},
        }

    def stream_chat(self, messages: list, tools: list | None = None,
                    temperature: float = 0.3, timeout: float = 120.0):
        if not self.api_key:
            raise LLMError("OpenAI Codex OAuth token 未配置")
        instructions, input_items = self._input(messages)
        converted_tools = self._tools(tools)
        last_error: LLMError | None = None
        for model in _model_candidates(self.model):
            payload: dict[str, Any] = {
                "model": model,
                "input": input_items,
                "stream": True,
                "store": False,
                "reasoning": {"summary": "auto"},   # GPT-5 思考摘要流（→ reasoning 事件，显示 ✻ 思考）
            }
            if instructions:
                payload["instructions"] = instructions
            if converted_tools:
                payload["tools"] = converted_tools
                payload["tool_choice"] = "auto"
            try:
                with httpx.stream("POST", f"{self.base_url}/responses",
                                  headers=self._headers(), json=payload, timeout=timeout) as resp:
                    if resp.status_code >= 400:
                        body = resp.read().decode("utf-8", "replace")
                        err = LLMError(f"Codex Responses stream HTTP {resp.status_code}: {body[:200]}")
                        if _unsupported_model_error(err):
                            last_error = err
                            continue
                        raise err
                    self.model = model
                    for ev in parse_responses_sse(resp.iter_lines()):
                        yield ev
                    return
            except httpx.HTTPError as exc:
                raise LLMError(f"Codex Responses 流式连接失败：{exc}") from exc
        raise last_error or LLMError("Codex Responses 没有可用模型")


def probe_codex(api_key: str, *, model: str = "gpt-5.5",
                base_url: str = "https://chatgpt.com/backend-api/codex",
                timeout: float = 30.0) -> dict[str, Any]:
    provider = CodexProvider(api_key, model, base_url)
    result = provider.chat(
        [{"role": "user", "content": "Reply with the single word OK."}],
        temperature=0.0,
        timeout=timeout,
    )
    return {
        "ok": True,
        "model": provider.model,
        "content": str(result.get("content") or "").strip(),
        "usage": result.get("usage") or {},
    }
