"""通用 OpenAI 兼容 provider —— 一份实现适配 OpenAI / DeepSeek / 通义 / Kimi /
GLM / 豆包 / MiniMax / OpenRouter / 自定义端点（只是 base_url 不同）。"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Iterator

import httpx

from .base import (
    LLMError, LLMProvider,
    RETRIES as _RETRIES, RETRYABLE_STATUS as _RETRYABLE, retry_backoff as _backoff,
)

# 推理型模型名特征（只有它们接受 reasoning_effort；给普通模型加会 400，故按名门控）
_REASONING_MODEL_RE = re.compile(
    r"(?:^|[-_/])(o[1345](?:-|$)|reasoner|qwq|deepseek-r1|-r1(?:-|$)|glm-z|thinking)", re.I)


def _reasoning_effort_for(model: str, effort: str) -> str | None:
    """把思考旋钮映射成 OpenAI reasoning_effort，仅对推理型模型返回值，否则 None(不加)。
    off→minimal、low/medium/high→同名、auto→None(用模型默认)。"""
    if not _REASONING_MODEL_RE.search(model or ""):
        return None
    return {"off": "minimal", "low": "low", "medium": "medium", "high": "high"}.get(effort or "")


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
        if delta.get("reasoning_content"):   # deepseek-reasoner 等的思考流 → 思考事件
            yield {"type": "reasoning", "text": delta["reasoning_content"]}
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

    def _headers(self, *, stream: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, payload: dict, timeout: float) -> dict:
        if not self.base_url:
            raise LLMError("base_url 未配置")
        headers = self._headers(stream=False)
        last = ""
        for attempt in range(_RETRIES):
            try:
                resp = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload, timeout=timeout)
            except httpx.HTTPError as exc:
                last = f"连接失败：{exc}"   # 网络错误可重试
            else:
                if resp.status_code == 200:
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in _RETRYABLE:
                    raise LLMError(last)   # 4xx 等不可重试，直接抛
            if attempt < _RETRIES - 1:
                time.sleep(_backoff(attempt))
        raise LLMError(f"已重试 {_RETRIES} 次仍失败：{last}")

    def complete(self, system: str, user: str, json_mode: bool = False,
                 temperature: float = 0.2, timeout: float = 60.0) -> str:
        payload = {"model": self.model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature, "stream": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(payload, timeout)   # _post 已内置重试/退避
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        # Prompt caching：OpenAI/DeepSeek 等兼容服务对稳定前缀（system + tools）做服务端
        # 自动缓存，无需也不接受请求侧 cache 提示（不同于 Anthropic 的 cache_control）。
        # 我们保证 system 消息与 tools 在会话内稳定（见 cli._sys_msg / agent_tools.TOOL_SCHEMAS），
        # 命中率体现在 usage.prompt_cache_hit_tokens / prompt_tokens_details.cached_tokens。
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "stream": False}
        _eff = _reasoning_effort_for(self.model, self.reasoning_effort)
        if _eff:
            payload["reasoning_effort"] = _eff   # 思考深度旋钮（仅推理型模型）
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
        if not self.base_url:
            raise LLMError("base_url 未配置")
        headers = self._headers(stream=True)
        payload: dict[str, Any] = {"model": self.model, "messages": messages,
                                   "temperature": temperature, "stream": True,
                                   "stream_options": {"include_usage": True}}
        _eff = _reasoning_effort_for(self.model, self.reasoning_effort)
        if _eff:
            payload["reasoning_effort"] = _eff   # 思考深度旋钮（仅推理型模型）
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        last = ""
        for attempt in range(_RETRIES):
            yielded = False
            retryable = False
            try:
                with httpx.stream("POST", f"{self.base_url}/chat/completions",
                                  headers=headers,
                                  json=payload, timeout=timeout) as resp:
                    if resp.status_code != 200:
                        last = f"HTTP {resp.status_code}: {resp.read().decode('utf-8', 'replace')[:200]}"
                        retryable = resp.status_code in _RETRYABLE
                    else:
                        for ev in parse_sse(resp.iter_lines()):
                            yielded = True
                            yield ev
                        return   # 流式正常结束
            except httpx.HTTPError as exc:
                last = f"流式连接失败：{exc}"
                retryable = True
            if yielded:                       # 已吐内容，不能安全重试/降级
                raise LLMError(last)
            if retryable and attempt < _RETRIES - 1:
                time.sleep(_backoff(attempt))
                continue
            raise LLMError(last)


def probe_openai_compat(token: str, *, model: str, base_url: str, timeout: float = 30.0) -> dict[str, Any]:
    """Run a minimal live chat/completions request for OpenAI-compatible providers."""
    if not token:
        raise LLMError("token 未配置")
    provider = OpenAICompatProvider(token, model=model, base_url=base_url)
    data = provider._post({
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply with OK only."},
            {"role": "user", "content": "ping"},
        ],
        "temperature": 0,
        "stream": False,
    }, timeout)
    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    return {
        "ok": True,
        "model": model,
        "content": (msg.get("content") or "").strip(),
        "usage": data.get("usage") or {},
    }
