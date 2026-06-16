"""通用 OpenAI 兼容 provider —— 一份实现适配 OpenAI / DeepSeek / 通义 / Kimi /
GLM / 豆包 / MiniMax / OpenRouter / 自定义端点（只是 base_url 不同）。"""
from __future__ import annotations

import json

import httpx

from .base import LLMError, LLMProvider


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
