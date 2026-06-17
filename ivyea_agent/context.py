"""上下文管理：长对话压缩（对标 Claude Code /compact）。

把除 system 外的历史消息 LLM 摘要成一段，替换原历史 —— 既保住关键事实/决策/数字，
又把 token 压下来。整段替换避免破坏 OpenAI 的 assistant.tool_calls↔tool 配对。
"""
from __future__ import annotations

from typing import Any, Optional

from . import config

# 默认：上一轮 prompt_tokens 超过此值就自动压缩（deepseek-chat 上限 64K，留足余量）
DEFAULT_COMPACT_AT = 48000

_SUMMARY_SYS = "你是对话压缩器。把给定的多轮对话压缩成简洁要点，必须保留：关键事实、已做的决策、ASIN/店铺SID/具体数字、用户偏好与未完成事项。用中文分条，不要寒暄。"


def _render_history(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        content = m.get("content")
        if role == "assistant" and m.get("tool_calls"):
            names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
            parts.append(f"[助手调用工具] {names}")
        if content:
            parts.append(f"[{role}] {content}")
        if role == "tool":
            parts.append(f"[工具结果] {str(content)[:500]}")
    return "\n".join(parts)


def should_compact(last_prompt_tokens: int, threshold: Optional[int] = None) -> bool:
    th = threshold if threshold is not None else int(config.get_setting("compact_at_tokens", DEFAULT_COMPACT_AT))
    return last_prompt_tokens > th


def compact(messages: list[dict], provider, *, keep_system: bool = True) -> tuple[list[dict], str]:
    """把历史压成摘要。返回 (新消息列表, 摘要文本)。失败则原样返回。
    新列表 = [system?, {user: 摘要}]，干净无 tool 配对残留。"""
    system = messages[0] if (messages and messages[0].get("role") == "system") else None
    history = messages[1:] if system else messages
    if len(history) < 4:
        return messages, ""   # 太短不值得压
    text = _render_history(messages)
    try:
        summary = provider.complete(_SUMMARY_SYS, text, temperature=0.2, timeout=120.0)
    except Exception:
        return messages, ""
    if not summary.strip():
        return messages, ""
    new: list[dict] = []
    if system and keep_system:
        new.append(system)
    new.append({"role": "user", "content": f"[此前对话摘要，请据此继续]\n{summary.strip()}"})
    new.append({"role": "assistant", "content": "（已读取摘要，请继续。）"})
    return new, summary.strip()
