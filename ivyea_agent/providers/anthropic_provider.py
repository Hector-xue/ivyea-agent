"""Anthropic Claude 原生 provider（用官方 anthropic SDK）。

把 agent 内部统一的 OpenAI 格式消息/工具翻译成 Anthropic Messages API 的内容块协议，
再把 Anthropic 响应翻译回统一格式（content/tool_calls/usage）。支持流式与 prompt caching。

模型 ID 用裸串：claude-opus-4-8（默认/最强）、claude-sonnet-4-6、claude-haiku-4-5。
价格见 pricing.py。需 ANTHROPIC_API_KEY（或自有兼容网关 base_url）。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .base import LLMError, LLMProvider

_DEFAULT_MAX_TOKENS = 8192


def _split_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI 格式 → (system 文本, Anthropic messages)。

    - system 角色 → 汇成顶层 system 串
    - assistant.tool_calls → content 里的 tool_use 块
    - role=tool → 合并成 user 消息里的 tool_result 块（连续的合并到一条）
    """
    system_parts: list[str] = []
    out: list[dict] = []

    def _append_user_block(block: dict) -> None:
        if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
            out[-1]["content"].append(block)
        else:
            out.append({"role": "user", "content": [block]})

    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
        elif role == "tool":
            _append_user_block({"type": "tool_result",
                                "tool_use_id": m.get("tool_call_id", ""),
                                "content": str(m.get("content", ""))})
        elif role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id", ""),
                               "name": fn.get("name", ""), "input": args})
            out.append({"role": "assistant", "content": blocks or ""})
        else:  # user
            content = m.get("content")
            if isinstance(content, str):
                _append_user_block({"type": "text", "text": content})
            elif isinstance(content, list):
                for b in content:
                    _append_user_block(b)
    return "\n\n".join(system_parts), out


def _tools_to_anthropic(tools: Optional[list]) -> Optional[list]:
    """OpenAI function tools → Anthropic tools（input_schema）。"""
    if not tools:
        return None
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({"name": fn.get("name", ""), "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}})})
    return out


def _system_param(system: str, cache: bool = True):
    """system 作 text 块并在末块打 cache_control（缓存 tools+system 前缀）。"""
    if not system:
        return None
    block: dict[str, Any] = {"type": "text", "text": system}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _norm_usage(usage: Any) -> dict:
    """Anthropic usage → 统一 usage（prompt/completion/cache_hit）。"""
    if not usage:
        return {}
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    inp = getattr(usage, "input_tokens", 0) or 0
    return {"prompt_tokens": inp + read + creation,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
            "prompt_cache_hit_tokens": read}


def _extract(message: Any) -> dict:
    """Anthropic Message → 统一 {content, tool_calls, usage}。"""
    text_parts, tool_calls = [], []
    for block in (message.content or []):
        bt = getattr(block, "type", None)
        if bt == "text":
            text_parts.append(block.text)
        elif bt == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input or {}})
    return {"role": "assistant", "content": "".join(text_parts),
            "tool_calls": tool_calls, "usage": _norm_usage(getattr(message, "usage", None))}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        super().__init__(api_key, model or "claude-opus-4-8")
        self.base_url = (base_url or "").rstrip("/")
        self._client = None

    def _cli(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise LLMError("未安装 anthropic SDK：pip install 'ivyea-agent[anthropic]'") from e
            if not self.api_key:
                raise LLMError("ANTHROPIC_API_KEY 未配置（ivyea model 选 Claude 并配 key）")
            kw: dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kw["base_url"] = self.base_url
            self._client = anthropic.Anthropic(**kw)
        return self._client

    def complete(self, system, user, json_mode=False, temperature=0.2, timeout=60.0):
        try:
            msg = self._cli().with_options(timeout=timeout).messages.create(
                model=self.model, max_tokens=_DEFAULT_MAX_TOKENS,
                system=_system_param(system),   # cache the (often-repeated) system prefix
                messages=[{"role": "user", "content": user}])
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Claude 调用失败：{e}") from e
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        system, msgs = _split_messages(messages)
        kw: dict[str, Any] = {"model": self.model, "max_tokens": _DEFAULT_MAX_TOKENS,
                              "system": _system_param(system), "messages": msgs}
        at = _tools_to_anthropic(tools)
        if at:
            kw["tools"] = at
        try:
            msg = self._cli().with_options(timeout=timeout).messages.create(**kw)
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Claude 调用失败：{e}") from e
        return _extract(msg)

    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        system, msgs = _split_messages(messages)
        kw: dict[str, Any] = {"model": self.model, "max_tokens": _DEFAULT_MAX_TOKENS,
                              "system": _system_param(system), "messages": msgs}
        at = _tools_to_anthropic(tools)
        if at:
            kw["tools"] = at
        try:
            with self._cli().with_options(timeout=timeout).messages.stream(**kw) as stream:
                for text in stream.text_stream:
                    yield {"type": "text", "text": text}
                final = stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Claude 流式失败：{e}") from e
        ex = _extract(final)
        yield {"type": "final", "content": ex["content"],
               "tool_calls": ex["tool_calls"], "usage": ex["usage"]}
