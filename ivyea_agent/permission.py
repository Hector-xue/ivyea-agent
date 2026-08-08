"""权限审批引擎（Claude Code 式）。

任何写动作执行前都必须经过它：给清晰预览 + 多档选项，远胜 y/n。
被 `apply`（命令式）和 `chat`（对话式）共用，保证写操作永远有人把关。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import tui, ui
from .actions import Action

APPROVE, DENY, ABORT = "approve", "deny", "abort"

# 审批提问通道。默认走 tui.select（终端菜单）；serve 注入一个把选项发到网页、
# 阻塞等前端回决策的实现，于是同一套审批引擎既服务 CLI 也服务工作台。
# 签名：(title, body, options, meta) -> 选中的 option key
# meta 带上终端菜单用不着、但网页确认卡需要的东西（op_type/destructive/摘要）。
PromptFn = Callable[[str, str, list, dict], str]


@dataclass
class PermissionState:
    """一次会话/一次 apply 内的审批状态。"""
    session_allow: set = field(default_factory=set)   # 本会话已"允许此类"的动作 kind
    aborted: bool = False
    accept_edits: bool = False                         # opt-in：本会话所有写操作自动放行（/auto-edit on）
    policy_auto: bool = False                          # --permission-mode policy：无人值守下按 policy.json
    #                                                    allow/deny 自动判定，不弹交互、永不 ABORT
    prompt_fn: PromptFn | None = None                  # 非 None = 走远程审批（网页确认卡），不碰 stdin


def _ask(state: "PermissionState", title: str, body: str, options: list,
         input_fn: Callable[[str], str], meta: dict | None = None) -> str:
    """问一次，拿一个决策。

    有 prompt_fn（远程）时不碰终端：远端没法做"改一下"这种就地编辑（要多轮
    交互输入），所以先把 edit 选项摘掉——宁可让用户取消后重新提要求，也不要
    给一个点了会卡在服务端 stdin 上的按钮。
    """
    if state.prompt_fn is not None:
        remote_options = [o for o in options if o[0] != "edit"]
        return state.prompt_fn(title, body, remote_options, dict(meta or {}))
    return tui.select(title, body, options, kind="warn", input_fn=input_fn)


def _policy_decide(intent: dict) -> str:
    """policy 档的无人值守判定：run_command 走 assess_command（deny 名单/高风险拦截），
    文件写走 check_path 写根校验；其余写类（execute_actions/领星写/run_python 等）一律 DENY。
    只返回 APPROVE/DENY——单工具拒绝不终止整轮（对比默认档非 tty 下首个写工具即 abort 全轮）。"""
    from . import policy
    op = str(intent.get("op_type") or "")
    if op == "run_command":
        cmd = str(intent.get("command") or "")
        if not cmd:
            return DENY
        return APPROVE if policy.assess_command(cmd).get("ok") else DENY
    if op in ("write_file", "edit_file", "code_apply_patch"):
        paths = [str(p) for p in (intent.get("paths") or []) if str(p)]
        if intent.get("path"):
            paths.append(str(intent["path"]))
        if not paths:
            return DENY
        return APPROVE if all(policy.check_path(p, "write")[0] for p in paths) else DENY
    return DENY


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
    if state.accept_edits or a.kind in state.session_allow:
        return APPROVE
    if state.policy_auto:
        return DENY   # policy 档不放行广告等域写动作（Action 类），且不弹交互

    options = [("approve", "批准本次"), ("session", "本会话同类都批准"),
               ("deny", "拒绝"), ("edit", "修改"), ("abort", "全部停止")]
    while True:
        choice = _ask(state, "需要确认写操作", preview(a), options, input_fn,
                      {"op_type": f"action:{a.kind}", "destructive": True,
                       "summary": a.summary()})
        if choice == "approve":
            return APPROVE
        if choice == "session":
            state.session_allow.add(a.kind)
            return APPROVE
        if choice == "deny":
            return DENY
        if choice == "abort":
            state.aborted = True
            return ABORT
        if choice == "edit":
            _edit(a, input_fn)
            print(ui.message("info", "已修改为：" + a.summary()))
            continue   # 改完重新确认


def request_intent(intent: dict, preview_text: str, state: PermissionState,
                   input_fn: Callable[[str], str] = _default_input,
                   edit_fn: Callable[[dict, Callable], None] = None) -> str:
    """通用写 intent 审批（领星等）。复用 session_allow（按 op_type）与 abort。
    返回 APPROVE/DENY/ABORT。[4]改 委托给 edit_fn（可选）。"""
    if state.aborted:
        return ABORT
    op_type = intent.get("op_type", "")
    if state.accept_edits or op_type in state.session_allow:
        return APPROVE
    if state.policy_auto:
        return _policy_decide(intent)   # 无人值守：按 policy.json 判定，不弹交互
    has_edit = edit_fn is not None
    options = [("approve", "批准本次"), ("session", "本会话同类都批准"), ("deny", "拒绝")]
    if has_edit:
        options.append(("edit", "修改"))
    options.append(("abort", "全部停止"))
    choice = _ask(state, "需要确认写操作", preview_text, options, input_fn,
                  {"op_type": op_type, "destructive": True})
    if choice == "approve":
        return APPROVE
    if choice == "session":
        state.session_allow.add(op_type)
        return APPROVE
    if choice == "deny":
        return DENY
    if choice == "abort":
        state.aborted = True
        return ABORT
    if choice == "edit" and has_edit:
        edit_fn(intent, input_fn)
        print(ui.message("info", "已修改，请重新确认。"))
        return "recheck"
    return DENY


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
