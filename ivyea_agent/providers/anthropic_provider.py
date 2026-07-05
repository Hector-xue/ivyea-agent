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
# Claude 订阅版 OAuth token 打 messages API 的已知硬要求：system 首块须是 Claude Code 身份，否则 401/403。
_CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
_THINK_BUDGET = {"low": 4096, "medium": 8192, "high": 16384}   # 思考旋钮 → extended thinking token 预算


def _thinking_kw(effort: str, base_max_tokens: int) -> dict:
    """思考旋钮 → Claude extended thinking 请求参数；off/auto 不开思考。
    Anthropic 要求 max_tokens > budget_tokens，故按需抬高 max_tokens。"""
    budget = _THINK_BUDGET.get(effort or "")
    if not budget:
        return {}
    return {"thinking": {"type": "enabled", "budget_tokens": budget},
            "max_tokens": budget + base_max_tokens}


def _split_messages(messages: list[dict], thinking_cache: dict | None = None) -> tuple[str, list[dict]]:
    """OpenAI 格式 → (system 文本, Anthropic messages)。

    - system 角色 → 汇成顶层 system 串
    - assistant.tool_calls → content 里的 tool_use 块
    - role=tool → 合并成 user 消息里的 tool_result 块（连续的合并到一条）
    - thinking_cache（按 tool_call id 缓存的 thinking 块）：开思考时，带工具的 assistant 轮须把
      thinking 块排在 tool_use 之前回传，否则 Anthropic API 400。
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
            tcs = m.get("tool_calls") or []
            if thinking_cache and tcs:   # thinking 块必须排在最前（Anthropic 硬要求）
                cached = thinking_cache.get(tcs[0].get("id", ""))
                if cached:
                    blocks.extend(cached)
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in tcs:
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
                    if isinstance(b, dict) and b.get("type") == "image_url":   # 多模态→Anthropic image 块
                        iu = b.get("image_url")
                        url = iu.get("url") if isinstance(iu, dict) else iu
                        if url and str(url).startswith("data:"):
                            head, b64 = str(url).split(",", 1)
                            media = head.split(":", 1)[1].split(";", 1)[0]
                            _append_user_block({"type": "image", "source": {
                                "type": "base64", "media_type": media, "data": b64}})
                        continue
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
    """Anthropic Message → 统一 {content, tool_calls, usage, _thinking}。
    _thinking：原样保留的 thinking/redacted_thinking 块（带 signature），供工具循环里回传（否则 API 400）。"""
    text_parts, tool_calls, thinking = [], [], []
    for block in (message.content or []):
        bt = getattr(block, "type", None)
        if bt == "text":
            text_parts.append(block.text)
        elif bt == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input or {}})
        elif bt == "thinking":
            thinking.append({"type": "thinking", "thinking": getattr(block, "thinking", ""),
                             "signature": getattr(block, "signature", "")})
        elif bt == "redacted_thinking":
            thinking.append({"type": "redacted_thinking", "data": getattr(block, "data", "")})
    return {"role": "assistant", "content": "".join(text_parts),
            "tool_calls": tool_calls, "usage": _norm_usage(getattr(message, "usage", None)),
            "_thinking": thinking}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str = "", oauth: bool = False):
        super().__init__(api_key, model or "claude-opus-4-8")
        self.base_url = (base_url or "").rstrip("/")
        self.oauth = oauth          # True=订阅版 OAuth：走 Bearer + oauth beta 头 + Claude Code 身份
        self._client = None
        self._thinking_cache: dict[str, list] = {}   # tool_call id → 该 assistant 轮的 thinking 块（工具循环回传用）

    def _cache_thinking(self, ex: dict) -> None:
        tcs = ex.get("tool_calls") or []
        th = ex.get("_thinking") or []
        if tcs and th:
            self._thinking_cache[tcs[0]["id"]] = th

    def _system(self, system: str):
        """system 块。OAuth 模式下首块必须是 Claude Code 身份（Max token 硬要求）。"""
        base = _system_param(system) or []
        if self.oauth:
            return [{"type": "text", "text": _CLAUDE_CODE_IDENTITY}] + base
        return base or None

    def _cli(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise LLMError("未安装 anthropic SDK：pip install 'ivyea-agent[anthropic]'") from e
            if not self.api_key:
                raise LLMError("Claude OAuth 未登录，先 `ivyea model auth anthropic-oauth --login`"
                               if self.oauth else "ANTHROPIC_API_KEY 未配置（ivyea model 选 Claude 并配 key）")
            kw: dict[str, Any] = {}
            if self.oauth:   # 订阅版：Bearer token + oauth beta 头（不传 api_key）
                from ..oauth_auth import ANTHROPIC_OAUTH_BETA
                kw["auth_token"] = self.api_key
                kw["default_headers"] = {"anthropic-beta": ANTHROPIC_OAUTH_BETA}
            else:
                kw["api_key"] = self.api_key
            if self.base_url:
                kw["base_url"] = self.base_url
            self._client = anthropic.Anthropic(**kw)
        return self._client

    def complete(self, system, user, json_mode=False, temperature=0.2, timeout=60.0):
        try:
            msg = self._cli().with_options(timeout=timeout).messages.create(
                model=self.model, max_tokens=_DEFAULT_MAX_TOKENS,
                system=self._system(system),   # cache the (often-repeated) system prefix；OAuth 加 Claude Code 身份
                messages=[{"role": "user", "content": user}])
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Claude 调用失败：{e}") from e
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        system, msgs = _split_messages(messages, self._thinking_cache)
        kw: dict[str, Any] = {"model": self.model, "max_tokens": _DEFAULT_MAX_TOKENS,
                              "system": self._system(system), "messages": msgs}
        kw.update(_thinking_kw(self.reasoning_effort, _DEFAULT_MAX_TOKENS))   # 思考深度旋钮
        at = _tools_to_anthropic(tools)
        if at:
            kw["tools"] = at
        try:
            msg = self._cli().with_options(timeout=timeout).messages.create(**kw)
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Claude 调用失败：{e}") from e
        ex = _extract(msg)
        self._cache_thinking(ex)
        return ex

    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        system, msgs = _split_messages(messages, self._thinking_cache)
        kw: dict[str, Any] = {"model": self.model, "max_tokens": _DEFAULT_MAX_TOKENS,
                              "system": self._system(system), "messages": msgs}
        kw.update(_thinking_kw(self.reasoning_effort, _DEFAULT_MAX_TOKENS))   # 思考深度旋钮
        at = _tools_to_anthropic(tools)
        if at:
            kw["tools"] = at
        try:
            with self._cli().with_options(timeout=timeout).messages.stream(**kw) as stream:
                for event in stream:   # 思考增量→reasoning 事件(显示 ✻ 思考)，正文增量→text
                    et = getattr(event, "type", "")
                    if et == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        dt = getattr(delta, "type", "")
                        if dt == "thinking_delta":
                            yield {"type": "reasoning", "text": getattr(delta, "thinking", "") or ""}
                        elif dt == "text_delta":
                            yield {"type": "text", "text": getattr(delta, "text", "") or ""}
                final = stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Claude 流式失败：{e}") from e
        ex = _extract(final)
        self._cache_thinking(ex)   # 缓存本轮 thinking 块，供下一步工具循环回传
        yield {"type": "final", "content": ex["content"],
               "tool_calls": ex["tool_calls"], "usage": ex["usage"]}
