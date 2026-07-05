"""通用自我批判层。

把广告域的 LLM 复核（review.py）泛化成任意任务的"收尾前自查一遍"：
给定任务与回答，按 rubric 逐维度找问题（答非所问/把猜测当事实/遗漏/未验证/风险）。
无模型 key 时优雅降级，不抛异常。
"""
from __future__ import annotations

from typing import Any, Optional

from .providers import LLMError

CRITIQUE_SYSTEM = ("你是严格、克制的复核者。对给定「任务」与「回答」做批判性自查，"
                   "只输出简短 Markdown，不复述原答案，不奉承。")

DEFAULT_RUBRIC = """按以下维度逐条查，发现问题才写、没问题略过：
1. 需求吻合：是否真正满足任务、有没有答非所问或漏掉子需求。
2. 事实可靠：数字/引用/结论有无把猜测当事实（应能标 [证据]/[推断]）。
3. 关键遗漏：漏掉的边界、风险、反例或前提。
4. 验证到位：若涉及代码/操作，是否真正验证过、有无副作用或安全隐患。
每条一句话、可执行。最后一行给结论：**通过** 或 **建议修正**（列 1-3 条最重要的）。"""


def critique(task: str, answer: str, provider, rubric: Optional[str] = None) -> dict[str, Any]:
    """返回 {ok, markdown, note}。provider 为 None（未配模型）时优雅降级，不抛异常。"""
    if provider is None:
        return {"ok": False, "markdown": "", "note": "未配置模型，无法自我批判。"}
    task = (task or "（未提供任务描述）").strip()
    answer = (answer or "").strip()
    if not answer:
        return {"ok": False, "markdown": "", "note": "没有可复核的回答内容。"}
    user = f"【任务】\n{task}\n\n【回答】\n{answer}\n\n{rubric or DEFAULT_RUBRIC}"
    try:
        md = provider.complete(CRITIQUE_SYSTEM, user, json_mode=False, temperature=0.2)
    except LLMError as e:
        return {"ok": False, "markdown": "", "note": f"自我批判调用失败：{e}"}
    return {"ok": True, "markdown": (md or "").strip(), "note": ""}
