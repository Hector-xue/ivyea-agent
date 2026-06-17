"""权限审批引擎（Claude Code 式）。

任何写动作执行前都必须经过它：给清晰预览 + 多档选项，远胜 y/n。
被 `apply`（命令式）和 `chat`（对话式）共用，保证写操作永远有人把关。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .actions import Action

APPROVE, DENY, ABORT = "approve", "deny", "abort"


@dataclass
class PermissionState:
    """一次会话/一次 apply 内的审批状态。"""
    session_allow: set = field(default_factory=set)   # 本会话已"允许此类"的动作 kind
    aborted: bool = False


def preview(a: Action) -> str:
    """人类可读的写动作预览（含 before→after）。"""
    head = a.summary()
    meta = (f"分类:{a.term_category or '?'} 置信:{a.confidence or '?'} | "
            f"30d {a.clicks30:.0f}点击/{a.orders30:.0f}单/ACOS {a.acos30 or '—'}")
    return f"{head}\n      {meta}\n      理由: {a.reason or '—'}"


def _default_input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "5"


def request(a: Action, state: PermissionState,
            input_fn: Callable[[str], str] = _default_input) -> str:
    """请求一个写动作的审批，返回 APPROVE/DENY/ABORT。
    会就地处理「本会话都允许此类」与「改一下」(改幅度/否定匹配)。"""
    if state.aborted:
        return ABORT
    if a.kind in state.session_allow:
        return APPROVE

    print("\n  需要确认写操作：")
    print("      " + preview(a))
    while True:
        choice = input_fn("  [1]是  [2]本会话都允许此类  [3]否  [4]改一下  [5]全部停: ")
        if choice in ("1", "y", "yes"):
            return APPROVE
        if choice == "2":
            state.session_allow.add(a.kind)
            return APPROVE
        if choice in ("3", "n", "no"):
            return DENY
        if choice in ("5", "q"):
            state.aborted = True
            return ABORT
        if choice == "4":
            _edit(a, input_fn)
            print("      改为：" + a.summary())
            continue   # 改完重新确认
        print("      请输入 1-5。")


def request_intent(intent: dict, preview_text: str, state: PermissionState,
                   input_fn: Callable[[str], str] = _default_input,
                   edit_fn: Callable[[dict, Callable], None] = None) -> str:
    """通用写 intent 审批（领星等）。复用 session_allow（按 op_type）与 abort。
    返回 APPROVE/DENY/ABORT。[4]改 委托给 edit_fn（可选）。"""
    if state.aborted:
        return ABORT
    op_type = intent.get("op_type", "")
    if op_type in state.session_allow:
        return APPROVE
    print("\n  需要确认写操作：")
    print("      " + preview_text)
    has_edit = edit_fn is not None
    opts = "  [1]是  [2]本会话都允许此类  [3]否  " + ("[4]改一下  " if has_edit else "") + "[5]全部停: "
    while True:
        choice = input_fn(opts)
        if choice in ("1", "y", "yes"):
            return APPROVE
        if choice == "2":
            state.session_allow.add(op_type)
            return APPROVE
        if choice in ("3", "n", "no"):
            return DENY
        if choice in ("5", "q"):
            state.aborted = True
            return ABORT
        if choice == "4" and has_edit:
            edit_fn(intent, input_fn)
            print("      已修改。")
            return "recheck"
        print("      请输入选项编号。")


def _edit(a: Action, input_fn: Callable[[str], str]) -> None:
    if a.kind == "negative":
        m = input_fn("      否定匹配 [negativeExact/negativePhrase] (回车不变): ").strip()
        if m in ("negativeExact", "negativePhrase"):
            a.negate_match = m
    else:  # 调价
        raw = input_fn("      新的调整幅度，如 -0.1 表示 -10% (回车不变): ").strip()
        if raw:
            try:
                pct = float(raw)
                if abs(pct) > 0.20:
                    print("      超过 ±20% 上限，未采用。")
                else:
                    a.change_pct = pct
                    if a.current_bid is not None:
                        a.new_bid = round(a.current_bid * (1 + pct), 2)
            except ValueError:
                print("      不是数字，未改。")
