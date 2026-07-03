"""上下文管理：长对话压缩（对标 Claude Code /compact）。

把除 system 外的历史消息 LLM 摘要成一段，替换原历史 —— 既保住关键事实/决策/数字，
又把 token 压下来。整段替换避免破坏 OpenAI 的 assistant.tool_calls↔tool 配对。
"""
from __future__ import annotations

import json
from typing import Optional

from . import config

# 默认主动自动压缩（对标 Claude Code）：prompt tokens 越过软阈值即在轮后压缩历史，
# 压缩时给提示。/compact auto off 可关。小上下文模型可 config set compact_at_tokens 调低。
DEFAULT_COMPACT_AT = 96000
DEFAULT_AUTO_COMPACT = True
# 轮内硬上限：无论是否开自动压缩，估算 token 越过它就强制压缩以防请求溢出报错。
# 这是“防崩”而非“省钱”，所以默认开启、阈值取得很高，正常长任务不会触发。
DEFAULT_HARD_CEILING = 200000
# 压缩时保留最近 N 条消息原文（对标 Claude Code）：紧接压缩后的几步最依赖近期细节
# （刚读的文件、刚给的路径），全摘要化会"失忆"重复劳动。config set compact_keep_recent 可调，0=全量摘要。
DEFAULT_KEEP_RECENT = 6

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
    if not bool(config.get_setting("auto_compact", DEFAULT_AUTO_COMPACT)):
        return False
    th = threshold if threshold is not None else int(config.get_setting("compact_at_tokens", DEFAULT_COMPACT_AT))
    return last_prompt_tokens > th


def should_warn_compact(last_prompt_tokens: int, threshold: Optional[int] = None) -> bool:
    """Return True when history is long enough to suggest manual /compact."""
    th = threshold if threshold is not None else int(config.get_setting("compact_at_tokens", DEFAULT_COMPACT_AT))
    return last_prompt_tokens > th


def _est_text(s: str) -> float:
    """按字符类估 token：CJK ≈ 0.75 token/字（chars//3 会低估近一半，防溢出方向不安全），
    其余（英文/代码/空白）≈ 3.8 字/token。"""
    cjk = sum(1 for ch in s if "一" <= ch <= "鿿")
    return cjk * 0.75 + (len(s) - cjk) / 3.8


def estimate_tokens(messages: list[dict]) -> int:
    """轮内粗略 token 估算（无需 provider 用量回报），CJK/其它分开计。"""
    total = 0.0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += _est_text(content)
        elif isinstance(content, list):   # 多模态：只计文本块（图片按 base64 长度算会高估几十倍）
            for b in content:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    total += _est_text(b["text"])
        for tc in m.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments") or ""
            total += _est_text(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))
    return int(total)


def should_compact_midturn(est_tokens: int, threshold: Optional[int] = None) -> bool:
    """轮内是否该压缩：越过硬上限一律压（防溢出崩溃）；开了自动压缩则到软阈值也压。"""
    ceiling = int(config.get_setting("compact_hard_ceiling_tokens", DEFAULT_HARD_CEILING))
    if est_tokens > ceiling:
        return True
    return should_compact(est_tokens, threshold)


def _pair_safe_split(history: list[dict], keep_recent: int) -> int:
    """返回切分下标 idx：history[:idx] 摘要、history[idx:] 原文保留。
    向前回退保证保留区不以 tool 开头——tool 消息必须紧跟它的 assistant.tool_calls，
    撕开配对会让 OpenAI 格式请求直接报错。回退方向是"多保不少保"，安全。"""
    idx = max(0, len(history) - max(0, keep_recent))
    while 0 < idx < len(history) and history[idx].get("role") == "tool":
        idx -= 1        # 回退到该 tool 串前面的 assistant(tool_calls) 上
    return idx


def compact(messages: list[dict], provider, *, keep_system: bool = True,
            keep_recent: Optional[int] = None) -> tuple[list[dict], str]:
    """把旧历史压成摘要、保留最近 keep_recent 条消息原文（在 tool 配对边界切分）。
    返回 (新消息列表, 摘要文本)。失败则原样返回。
    新列表 = [system?, {user: 摘要}, {assistant: 确认}] + 最近原文。keep_recent=0 即旧行为全量摘要。"""
    if keep_recent is None:
        try:
            keep_recent = int(config.get_setting("compact_keep_recent", DEFAULT_KEEP_RECENT))
        except (TypeError, ValueError):
            keep_recent = DEFAULT_KEEP_RECENT
    system = messages[0] if (messages and messages[0].get("role") == "system") else None
    history = messages[1:] if system else messages
    split = _pair_safe_split(history, keep_recent)
    if split < 4 <= len(history):
        split = _pair_safe_split(history, 0)   # 历史短但需要压（如防溢出）：退回全量摘要
    old, recent = history[:split], history[split:]
    if len(old) < 4:
        return messages, ""   # 太短不值得压
    text = _render_history(old)
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
    new.extend(recent)
    return new, summary.strip()
