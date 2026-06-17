"""通用 OpenAI 兼容 provider —— 一份实现适配 OpenAI / DeepSeek / 通义 / Kimi /
GLM / 豆包 / MiniMax / OpenRouter / 自定义端点（只是 base_url 不同）。"""
from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

import httpx

from .base import LLMError, LLMProvider


def parse_sse(lines: Iterable[str]) -> Iterator[dict]:
    """解析 OpenAI 兼容的 SSE 流，产出统一事件（纯函数，便于测试）：
      {"type":"text","text":...}              内容增量
      {"type":"final","content","tool_calls","usage"}  收尾（累计后的完整结果）
    tool_calls 增量按 index 累积（id/name 在首块、arguments 流式拼接）。
    """
    content_parts: list[str] = []
    tools: dict[int, dict] = {}
    usage: dict = {}
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
        except Exception:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            content_parts.append(delta["content"])
            yield {"type": "text", "text": delta["content"]}
        for tc in (delta.get("tool_calls") or []):
            idx = tc.get("index", 0)
            slot = tools.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
    final_tools = []
    for _, slot in sorted(tools.items()):
        try:
            args = json.loads(slot["arguments"] or "{}")
        except Exception:
            args = {}
        final_tools.append({"id": slot["id"], "name": slot["name"], "arguments": args})
    yield {"type": "final", "content": "".join(content_parts),
           "tool_calls": final_tools, "usage": usage}


class OpenAICompatProvider(LLMProvider):
    name = "openai-compat"

    def __init__(self, api_key: str, model: str, base_url: str):
        super().__init__(api_key, model)
        self.base_url = (base_url or "").rstrip("/")

    def _post(self, payload: dict, timeout: float) -> dict:
        if not self.api_key:
            raise LLMError("API key 未配置（用 /config 或 ivyea model 配置）")
        if not self.base_url:
            raise LLMError("base_url 未配置")
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"连接失败：{exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def complete(self, system: str, user: str, json_mode: bool = False,
                 temperature: float = 0.2, timeout: float = 60.0) -> str:
        payload = {"model": self.model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature, "stream": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last = None
        for _ in range(3):
            try:
                data = self._post(payload, timeout)
                return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            except LLMError as e:
                last = e
        raise LLMError(f"调用失败（已重试）：{last}")

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._post(payload, timeout)
        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        tool_calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            tool_calls.append({"id": tc.get("id", ""), "name": fn.get("name", ""), "arguments": args})
        return {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}

    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        """流式工具调用。产出 parse_sse 的事件流（text 增量 + final）。
        失败时抛 LLMError，调用方可回退到非流式 chat。"""
        if not self.api_key:
            raise LLMError("API key 未配置（用 /config 或 ivyea model 配置）")
        payload: dict[str, Any] = {"model": self.model, "messages": messages,
                                   "temperature": temperature, "stream": True,
                                   "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            with httpx.stream("POST", f"{self.base_url}/chat/completions",
                              headers={"Authorization": f"Bearer {self.api_key}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=timeout) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", "replace")[:200]
                    raise LLMError(f"HTTP {resp.status_code}: {body}")
                yield from parse_sse(resp.iter_lines())
        except httpx.HTTPError as exc:
            raise LLMError(f"流式连接失败：{exc}") from exc
