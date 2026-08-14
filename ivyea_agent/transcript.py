"""对话记录的公开投影 —— 把"注回给模型的话"从"给人看的记录"里摘掉。

主循环为了纠正模型，会往消息流里塞 `role=user` 的门禁提示（引用门禁、完成门禁、
汇报门禁、完成前自验证），上下文压缩还会塞一条 `role=user` 的历史摘要。这些对模型
是必需输入 —— 删了它模型就不会重写答案 —— 但对人来说根本不是对话内容。原样吐给
界面的后果是**用户自己没发过的消息出现在"我"这一侧**（IvyeaOps 任务台的绿气泡），
而被门禁打回的那版草稿还会当成一条正经回答并排摆出来，一轮问答显示成三份答案。

所以分工是：`messages` 该塞照塞、该落盘照落盘（resume 要靠它复原现场），出口处
（`service._public_messages`）过一遍 `strip_injected`，只有终稿进人眼。

**新增任何"注回给模型的 user 消息"都必须走 `gate_text()`**，marker 登记进
`_USER_MARKERS`，否则 tests/test_transcript.py 会红 —— 当初 IvyeaOps 前端各写一份
剥离清单、新门禁上线时没人同步，才漏出了那条绿气泡。

不管的另一半：追加在**真实 user 消息尾巴上**的注入（`[Ivyea Skill：…]`、
`[Ivyea 本地知识检索…]`）不是独立消息，删了会连用户原话一起删。那部分由展示端
按后缀截断（IvyeaOps 的 `lib/stripInjected.ts` 与 `console_sessions.clean_preview`）。
"""
from __future__ import annotations

from typing import Any

# ── 注回给模型的 user 消息，按前缀登记 ──────────────────────────────────────
CITATION_GATE = "[知识引用门禁]"        # agent_loop._citation_gate_feedback
COMPLETION_GATE = "[完成门禁]"          # agent_loop._verify_gate_feedback（行为类改动）
PROGRESS_GATE = "[汇报门禁]"            # progress_reporting.completion_feedback
VERIFY_GATE = "⚠ 完成前自验证发现问题"   # verify.gate 的反馈首行
COMPACT_SUMMARY = "[此前对话摘要，请据此继续]"   # context.compact 压缩后的摘要

_USER_MARKERS: tuple[str, ...] = (
    CITATION_GATE,
    COMPLETION_GATE,
    PROGRESS_GATE,
    VERIFY_GATE,
    COMPACT_SUMMARY,
)

# 压缩后跟在摘要后面那句固定的 assistant 应答（context.compact 造的，模型没说过）。
COMPACT_ACK = "（已读取摘要，请继续。）"


def gate_text(marker: str, body: str) -> str:
    """拼一条注回给模型的门禁文本。marker 必须是登记过的前缀。

    body 自带前导分隔（空格或标点）—— 这样各处门禁的原文一个字节都不用改，
    同时把"这条消息是注入的"这件事变成登记制，而不是靠展示端猜前缀。
    """
    if marker not in _USER_MARKERS:
        raise ValueError(f"未登记的门禁标记：{marker!r}；请加进 transcript._USER_MARKERS")
    return f"{marker}{body}"


def _text_of(content: Any) -> str:
    """取消息正文的纯文本。多模态消息是 [{type,text},{type,image_url}] 这种列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def is_injected_user_message(content: Any) -> bool:
    """这条 user 消息是主循环注回的，不是用户打的字。"""
    return _text_of(content).lstrip().startswith(_USER_MARKERS)


def strip_injected(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """摘掉注回的 user 消息、以及被它打回的那版 assistant 草稿。

    草稿的识别不靠猜：门禁只在"模型这一步不再调工具、直接给文字"时才触发
    （见 agent_loop.run_turn），所以被打回的草稿**必然是紧挨在门禁消息前面、
    且不带 tool_calls 的那条 assistant**。带 tool_calls 的消息一律不动 ——
    它和后面的 tool 结果是成对的，拆开会把工具时间线打断。

    门禁重试到达上限时不会再注入，最后那版草稿就是终稿，自然留下。
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and is_injected_user_message(content):
            if out and out[-1].get("role") == "assistant" and not out[-1].get("tool_calls"):
                out.pop()
            continue
        if role == "assistant" and _text_of(content).strip() == COMPACT_ACK:
            continue
        out.append(msg)
    return out


def turn_slices(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """把（已经 strip_injected 过的）消息按轮切成 [start, end) 区间。

    一轮 = 一条真实的用户提问，加上它引出的全部 assistant / tool 消息，直到下一条提问。
    历史详情按轮分页要靠它 —— 按**消息条数**分页是这次 bug 的成因：一次提问能产生几十条
    消息，末 N 条里全是工具调用，用户自己发的那句话反而被挤出去了。

    第一条用户消息之前的内容（system、导入进来的开场）归到第 0 轮里，不单独成轮 ——
    它不是一次提问。
    """
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not starts:
        return [(0, len(messages))] if messages else []
    bounds = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(messages)
        bounds.append((0 if idx == 0 else start, end))
    return bounds


def visible_turns(messages: list[dict[str, Any]]) -> int:
    """真实的用户轮数。直接数 role=user 会把门禁消息也算成一轮。"""
    return sum(1 for m in messages
               if m.get("role") == "user" and not is_injected_user_message(m.get("content")))
