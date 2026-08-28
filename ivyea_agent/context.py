"""上下文管理：长对话压缩（对标 Claude Code /compact）。

把除 system 外的历史消息 LLM 摘要成一段，替换原历史 —— 既保住关键事实/决策/数字，
又把 token 压下来。整段替换避免破坏 OpenAI 的 assistant.tool_calls↔tool 配对。
"""
from __future__ import annotations

import json
from typing import Optional

from . import config, transcript

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
# 一次压缩至少要能吃掉这么多 token 才值得跑。压缩本身是一次真花钱的模型调用，
# 而且把还能直接用的近期原文换成散文摘要 —— 压不动还硬压是净亏。
#
# 这条闸真正防的是"反复空压"：`compact_at_tokens` 一旦被调到比 system 提示词本身
# 还低（小上下文模型、或手滑填了个小数字），用量就**永远**在阈值之上 —— 压缩动不了
# system，于是每一步都判定"该压了"、每一步都压不下去，来回烧钱还把历史反复摘要化。
# 判据必须是"这次能压掉多少"，不是"现在用了多少"。
MIN_COMPACTIBLE_TOKENS = 2000

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


def _keep_recent_setting(keep_recent: Optional[int]) -> int:
    if keep_recent is not None:
        return keep_recent
    try:
        return int(config.get_setting("compact_keep_recent", DEFAULT_KEEP_RECENT))
    except (TypeError, ValueError):
        return DEFAULT_KEEP_RECENT


def _split_for_compaction(messages: list[dict], keep_recent: Optional[int] = None):
    """返回 (system, 待摘要段, 原样保留段)。`compact` 与 `compactible_tokens` 共用同一套
    切分 —— 判定"值不值得压"和"实际压什么"必须是同一段，否则闸门和执行会各说各话。"""
    keep_recent = _keep_recent_setting(keep_recent)
    system = messages[0] if (messages and messages[0].get("role") == "system") else None
    history = messages[1:] if system else messages
    split = _pair_safe_split(history, keep_recent)
    if split < 4 <= len(history):
        split = _pair_safe_split(history, 0)   # 历史短但需要压（如防溢出）：退回全量摘要
    return system, history[:split], history[split:]


def compactible_tokens(messages: list[dict], keep_recent: Optional[int] = None) -> int:
    """这次压缩**能吃掉**多少 token：system 与保留区之外的那一段。

    压缩永远动不了 system（`keep_system=True`），也不动保留区。所以"用量越过阈值"
    从来就不等于"压缩帮得上忙"。太短不值得压的那一段返回 0。
    """
    _system, old, _recent = _split_for_compaction(messages, keep_recent)
    return estimate_tokens(old) if len(old) >= 4 else 0


def worth_compacting(messages: list[dict], keep_recent: Optional[int] = None) -> bool:
    """**自动**压缩该不该跑。手动 `/compact` 不问这一句 —— 用户明确要求就照跑。

    `should_compact*` 回答的是"用量到没到阈值"，这里回答的是另一个问题："压了有用吗"。
    两个都点头才动手，否则阈值被调到 system 提示词以下时会陷入反复空压。
    """
    return compactible_tokens(messages, keep_recent) >= MIN_COMPACTIBLE_TOKENS


def compact(messages: list[dict], provider, *, keep_system: bool = True,
            keep_recent: Optional[int] = None, extra_note: str = "",
            return_usage: bool = False):
    """把旧历史压成摘要、保留最近 keep_recent 条消息原文（在 tool 配对边界切分）。
    返回 (新消息列表, 摘要文本)。失败则原样返回。
    新列表 = [system?, {user: 摘要}, {assistant: 确认}] + 最近原文。keep_recent=0 即旧行为全量摘要。

    extra_note：**原样**接在摘要后面的结构化状态（当前用于任务计划）。摘要是散文、
    会走样，而"计划到第几步了"是状态，压一次就该原样过一次，不能交给模型复述。

    return_usage=True 时返回 (新消息列表, 摘要, 用量估算) 三元组 —— 压缩是**运行时自己
    发起**的一次模型调用，钱是真花的，成本闸不能把它漏在外面。默认 False，老调用方不变。"""

    def _out(msgs, summary, usage=None):
        return (msgs, summary, usage or {}) if return_usage else (msgs, summary)

    system, old, recent = _split_for_compaction(messages, keep_recent)
    if len(old) < 4:
        return _out(messages, "")   # 太短不值得压
    text = _render_history(old)
    try:
        summary = provider.complete(_SUMMARY_SYS, text, temperature=0.2, timeout=120.0)
    except Exception:
        return _out(messages, "")
    if not summary.strip():
        return _out(messages, "")
    new: list[dict] = []
    if system and keep_system:
        new.append(system)
    body = summary.strip()
    if extra_note.strip():
        body += "\n\n" + extra_note.strip()
    new.append({"role": "user",
                "content": transcript.gate_text(transcript.COMPACT_SUMMARY, f"\n{body}")})
    new.append({"role": "assistant", "content": transcript.COMPACT_ACK})
    new.extend(recent)
    # 压缩前把这段对话里值得长期记住的东西捞出来。
    #
    # **不压缩就丢了**：被压掉的那一段往往正是"结论是怎么来的"，而摘要只进这一条
    # 会话的上下文、换个会话就没了。这里复用刚生成的 summary 而不是把 old 消息再
    # 喂一遍模型 —— 同一段对话没必要付两次钱，而且摘要本身已经提炼过。
    try:
        from . import memory_reflect
        memory_reflect.reflect_summary_async(summary.strip())
    except Exception:  # noqa: BLE001 —— 记忆是锦上添花，压缩绝不能因它失败
        pass
    return _out(new, summary.strip(), {"prompt_tokens": int(_est_text(text)),
                                       "completion_tokens": int(_est_text(summary))})


# ── 上下文用量快照 ─────────────────────────────────────────────────────────
# 给调用方（IvyeaOps 任务台的上下文进度条）回答一件事：**这条会话把窗口用掉多少了**。
#
# 为什么不直接用服务商回报的 prompt_tokens：那是"上一次调用花了多少"，会话刚开、
# 或本轮还没发出去时它根本不存在，而进度条要在发第一句话之前就说得出话。所以这里
# 用 estimate_tokens 现算，并且**明说是估算**（estimated=True）——一个标着"估算"的
# 数比一个看起来精确其实来路不明的数诚实得多。
DEFAULT_WINDOW = 128_000

# 模型 → 上下文窗口。按 id 子串匹配，长的先匹配（gpt-4.1 要先于 gpt-4）。
# 查不到就落 DEFAULT_WINDOW —— 宁可把窗口说小（进度条偏保守），也不要凭空说成 1M。
_WINDOW_HINTS: tuple[tuple[str, int], ...] = (
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("gpt-5", 400_000),
    ("o3", 200_000),
    ("claude", 200_000),
    ("gemini", 1_000_000),
    ("kimi", 256_000),
    ("moonshot", 256_000),
    ("qwen", 128_000),
    ("glm", 128_000),
    ("deepseek", 128_000),
    ("llama", 128_000),
)


def window_for(model: str) -> int:
    """这个模型的上下文窗口。config 的 context_window 覆盖一切（换了个窗口不一样的
    自建模型时，用户改一行配置就能让进度条说对）。"""
    try:
        override = int(config.get_setting("context_window", 0) or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        return override
    name = str(model or "").lower()
    for key, win in _WINDOW_HINTS:
        if key in name:
            return win
    return DEFAULT_WINDOW


def _tool_tokens(tools: Optional[list]) -> int:
    """工具定义也要占窗口，而且占得不少（全量 54 个工具 ≈ 6.9K token，每一步都重发）。
    tools=None 表示"交给 agent_loop 兜底成全量"，这里跟着按全量算，否则进度条会把
    最大的一块漏掉。"""
    schemas = tools
    if schemas is None:
        try:
            from . import agent_tools
            schemas = agent_tools.TOOL_SCHEMAS
        except Exception:  # noqa: BLE001 — 取不到工具表只是少算一项，不该炸掉整轮
            return 0
    if not schemas:
        return 0
    try:
        return int(_est_text(json.dumps(schemas, ensure_ascii=False)))
    except (TypeError, ValueError):
        return 0


def snapshot(messages: list[dict], tools: Optional[list] = None, model: str = "") -> dict:
    """上下文占用快照：{used, window, percent, breakdown{system,tools,messages}, estimated}。

    分三档是因为"用满了"的成因完全不同：系统提示词大 = 人设/板块桥太长，工具大 =
    挂了全量工具，对话消息大 = 该压缩了。只报一个总数的话，用户只知道满了、不知道
    该动哪里。
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]
    sys_tok = estimate_tokens(system_msgs)
    msg_tok = estimate_tokens(other_msgs)
    tool_tok = _tool_tokens(tools)
    used = sys_tok + msg_tok + tool_tok
    window = window_for(model)
    return {
        "used": used,
        "window": window,
        "percent": round(used * 100.0 / window, 2) if window > 0 else 0.0,
        "breakdown": {"system": sys_tok, "tools": tool_tok, "messages": msg_tok},
        "estimated": True,
        "model": str(model or ""),
    }
