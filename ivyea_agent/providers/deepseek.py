"""DeepSeek 适配器（OpenAI 兼容 /chat/completions）。

同一套实现也适用于其它 OpenAI 兼容端点（改 base_url 即可），日后可抽成
通用 OpenAICompatProvider 复用给 Moonshot / 本地 vLLM 等。
"""
from __future__ import annotations

import json

import httpx

from .base import LLMError, LLMProvider

DEEPSEEK_BASE = "https://api.deepseek.com"


class DeepSeekProvider(LLMProvider):
    name = "deepseek"

    def complete(self, system: str, user: str, json_mode: bool = False,
                 temperature: float = 0.2, timeout: float = 60.0) -> str:
        if not self.api_key:
            raise LLMError("DeepSeek API key 未配置（ivyea config 或 ~/.ivyea/.env）")
        payload = {
            "model": self.model or "deepseek-chat",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last_err = None
        for attempt in range(3):
            try:
                resp = httpx.post(
                    f"{DEEPSEEK_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=timeout,
                )
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    continue
                data = resp.json()
                return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
        raise LLMError(f"DeepSeek 调用失败（已重试）：{last_err}")
