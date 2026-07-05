"""终端视觉面板：Todo/Plan 面板 + 彩色 Diff（对标 Claude Code）。

纯渲染，无副作用，便于测试。
"""
from __future__ import annotations

import difflib
import shutil
import textwrap

from . import terminal_theme, ui

_X = "\033[0m"
_DIM, _B = "\033[2m", "\033[1m"
_GREEN, _RED, _CYAN, _YEL = "\033[32m", "\033[31m", "\033[36m", "\033[33m"

# 状态 → (图标, 颜色)
_TODO = {
    "completed": ("☑", _DIM + _GREEN),
    "in_progress": ("◐", _B + _YEL),
    "pending": ("☐", _DIM),
    "blocked": ("✗", _B + _RED),
    "skipped": ("⊘", _DIM),
}


def render_todos(todos: list, *, color: bool = True) -> str:
    """todos: [{content, status: pending|in_progress|completed}] → 面板。"""
    if not todos:
        return ""
    color = terminal_theme.color_enabled(color)
    width = max(36, min(shutil.get_terminal_size((88, 24)).columns, 100))
    body_width = max(20, width - 5)
    done = sum(1 for t in todos if t.get("status") == "completed")
    head = f"{_CYAN if color else ''}╭─ 计划 {done}/{len(todos)}{_X if color else ''}"
    lines = [head]
    for t in todos:
        st = t.get("status", "pending")
        icon, c = _TODO.get(st, ("☐", _DIM))
        c = c if color else ""
        x = _X if color else ""
        bar = f"{_CYAN}│{_X} " if color else "│ "
        wrapped = textwrap.wrap(str(t.get("content", "")), width=body_width) or [""]
        lines.append(f"{bar}{c}{icon} {wrapped[0]}{x}")
        for cont in wrapped[1:]:
            lines.append(f"{bar}{c}  {cont}{x}")
    lines.append(f"{_CYAN if color else ''}╰─{_X if color else ''}")
    return "\n".join(lines)


def _report_rows(label: str, items: list | None) -> list[str]:
    clean = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not clean:
        return []
    return [f"{label}：{clean[0]}"] + [f"  - {item}" for item in clean[1:]]


def render_progress(event: dict, *, color: bool | None = None) -> str:
    """Render one structured start/phase/final progress event."""
    if not event:
        return ""
    kind = event.get("kind")
    lines: list[str] = []
    title = "任务进度"
    panel_kind = "info"
    if kind == "start":
        title = "开始执行"
        lines.append(f"目标：{event.get('objective') or '未说明'}")
        lines.extend(_report_rows("范围", event.get("scope")))
        plan = event.get("plan") or []
        if plan:
            lines.append("阶段：")
            lines.extend(f"  {i}. {item}" for i, item in enumerate(plan, 1))
        lines.extend(_report_rows("完成标准", event.get("success_criteria")))
        lines.append(f"当前：第 {event.get('phase_index')} 阶段 · {event.get('phase')}")
        if event.get("summary"):
            lines.append(f"准备做：{event.get('summary')}")
    elif kind == "phase_start":
        title = f"阶段 {event.get('phase_index')} 开始"
        lines = [f"阶段：{event.get('phase')}", f"准备做：{event.get('summary')}"]
    elif kind == "phase_end":
        status = event.get("status") or "completed"
        title = f"阶段 {event.get('phase_index')} 结束 · {status}"
        panel_kind = "success" if status == "completed" else ("error" if status == "blocked" else "warn")
        lines = [f"阶段：{event.get('phase')}", f"做了什么：{event.get('summary')}"]
        lines.extend(_report_rows("已做到", event.get("completed")))
        lines.extend(_report_rows("未做到", event.get("incomplete")))
        lines.extend(_report_rows("证据", event.get("evidence")))
        lines.extend(_report_rows("注意", event.get("attention")))
        if event.get("next"):
            lines.append(f"下一步：{event.get('next')}")
    elif kind == "final":
        title = "执行汇总"
        incomplete = [item for item in (event.get("incomplete") or []) if str(item).strip() != "无"]
        panel_kind = "warn" if incomplete else "success"
        lines = [f"整体结果：{event.get('summary')}"]
        lines.extend(_report_rows("已做到", event.get("completed")))
        lines.extend(_report_rows("未做到", event.get("incomplete")))
        lines.extend(_report_rows("验证", event.get("evidence")))
        lines.extend(_report_rows("注意", event.get("attention")))
    return ui.panel(title, lines or ["（无内容）"], kind=panel_kind, color=color)


def colorize_patch(patch: str, *, color: bool = True) -> str:
    """给一段 git 统一 diff 文本上色（绿增/红删/dim hunk头/青文件头）。用于 /diff 等。"""
    if not patch.strip():
        return ""
    color = terminal_theme.color_enabled(color)
    out = []
    current_path = ""
    for ln in patch.splitlines():
        if not color:
            out.append(ln)
        elif ln.startswith("diff --git") or ln.startswith(("+++", "---")):
            out.append(f"{_CYAN}{ln}{_X}")
            if ln.startswith("+++ "):
                current_path = ln[4:].strip()
                if current_path.startswith("b/"):
                    current_path = current_path[2:]
        elif ln.startswith("@@"):
            out.append(f"{_DIM}{ln}{_X}")
        elif ln.startswith("+"):
            out.append(f"{_GREEN}+{_X}{terminal_theme.highlight_line(ln[1:], filename=current_path)}")
        elif ln.startswith("-"):
            out.append(f"{_RED}-{_X}{terminal_theme.highlight_line(ln[1:], filename=current_path)}")
        else:
            out.append(ln)
    return "\n".join(out)


def render_diff(old: str, new: str, path: str = "", *, color: bool = True, context: int = 2) -> str:
    """old→new 的彩色 diff（对标 Claude Code）：左侧行号栏 + +/- 标记 + 语法高亮。

    每行自带不跨行的 ANSI，便于被审批菜单逐行解析配色。color=False 时纯文本（测试/管道）。
    """
    if old == new:
        return "（无变化）"
    color = terminal_theme.color_enabled(color)
    a, b = old.splitlines(), new.splitlines()
    if color:
        a_hl = terminal_theme.highlight_code("\n".join(a), filename=path, color=True).split("\n") if a else []
        b_hl = terminal_theme.highlight_code("\n".join(b), filename=path, color=True).split("\n") if b else []
        if len(a_hl) != len(a):                 # formatter/lexer edge case: preserve row mapping
            a_hl = list(a)
        if len(b_hl) != len(b):
            b_hl = list(b)
    else:
        a_hl, b_hl = list(a), list(b)
    sm = difflib.SequenceMatcher(None, a, b)

    def row(num, sign: str, code: str, highlighted: str) -> str:
        gutter = f"{num:>4}" if num else "    "
        if not color:
            return f"{gutter} {sign} {code}".rstrip()
        sign_c = {"+": _GREEN, "-": _RED}.get(sign, "")
        gutter_c = sign_c or _DIM
        return f"{gutter_c}{gutter}{_X} {sign_c}{sign or ' '}{_X} {highlighted}".rstrip()

    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            seg = list(range(i1, i2))
            if len(seg) > context * 2 + 1:          # 大段未变只留首尾 context 行，中间折叠
                seg = seg[:context] + [None] + seg[-context:]
            for k in seg:
                if k is None:
                    out.append(f"{_DIM}     ⋮{_X}" if color else "     ⋮")
                else:
                    out.append(row(j1 + (k - i1) + 1, " ", a[k], a_hl[k]))
            continue
        if tag in ("replace", "delete"):
            for k in range(i1, i2):
                out.append(row(k + 1, "-", a[k], a_hl[k]))
        if tag in ("replace", "insert"):
            for k in range(j1, j2):
                out.append(row(k + 1, "+", b[k], b_hl[k]))
    return "\n".join(out)
