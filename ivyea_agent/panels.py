"""终端视觉面板：Todo/Plan 面板 + 彩色 Diff（对标 Claude Code）。

纯渲染，无副作用，便于测试。
"""
from __future__ import annotations

import difflib
import shutil
import textwrap

_X = "\033[0m"
_DIM, _B = "\033[2m", "\033[1m"
_GREEN, _RED, _CYAN, _YEL = "\033[32m", "\033[31m", "\033[36m", "\033[33m"

# 状态 → (图标, 颜色)
_TODO = {
    "completed": ("☑", _DIM + _GREEN),
    "in_progress": ("◐", _B + _CYAN),
    "pending": ("☐", _DIM),
}


def render_todos(todos: list, *, color: bool = True) -> str:
    """todos: [{content, status: pending|in_progress|completed}] → 面板。"""
    if not todos:
        return ""
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


def colorize_patch(patch: str, *, color: bool = True) -> str:
    """给一段 git 统一 diff 文本上色（绿增/红删/dim hunk头/青文件头）。用于 /diff 等。"""
    if not patch.strip():
        return ""
    out = []
    for ln in patch.splitlines():
        if not color:
            out.append(ln)
        elif ln.startswith("diff --git") or ln.startswith(("+++", "---")):
            out.append(f"{_CYAN}{ln}{_X}")
        elif ln.startswith("@@"):
            out.append(f"{_DIM}{ln}{_X}")
        elif ln.startswith("+"):
            out.append(f"{_GREEN}{ln}{_X}")
        elif ln.startswith("-"):
            out.append(f"{_RED}{ln}{_X}")
        else:
            out.append(ln)
    return "\n".join(out)


def _diff_lexer(path: str):
    """按文件名取 pygments lexer；不可用/未知则 None（退化为纯文本）。"""
    if not path:
        return None
    try:
        from pygments.lexers import get_lexer_for_filename
        return get_lexer_for_filename(path, stripnl=False)
    except Exception:
        return None


def _hl(code: str, lexer) -> str:
    """对单行代码做语法高亮（保留行内不跨行的 ANSI）；无 lexer 原样返回。"""
    if lexer is None or not code:
        return code
    try:
        from pygments import highlight
        from pygments.formatters import TerminalFormatter
        return highlight(code, lexer, TerminalFormatter()).rstrip("\n")
    except Exception:
        return code


def render_diff(old: str, new: str, path: str = "", *, color: bool = True, context: int = 2) -> str:
    """old→new 的彩色 diff（对标 Claude Code）：左侧行号栏 + +/- 标记 + 语法高亮。

    每行自带不跨行的 ANSI，便于被审批菜单逐行解析配色。color=False 时纯文本（测试/管道）。
    """
    if old == new:
        return "（无变化）"
    a, b = old.splitlines(), new.splitlines()
    lexer = _diff_lexer(path) if color else None
    sm = difflib.SequenceMatcher(None, a, b)

    def row(num, sign: str, code: str) -> str:
        gutter = f"{num:>4}" if num else "    "
        hl = _hl(code, lexer)
        if not color:
            return f"{gutter} {sign} {code}".rstrip()
        sign_c = {"+": _GREEN, "-": _RED}.get(sign, "")
        return f"{_DIM}{gutter}{_X} {sign_c}{sign or ' '}{_X} {hl}".rstrip()

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
                    out.append(row(j1 + (k - i1) + 1, " ", a[k]))
            continue
        if tag in ("replace", "delete"):
            for k in range(i1, i2):
                out.append(row(k + 1, "-", a[k]))
        if tag in ("replace", "insert"):
            for k in range(j1, j2):
                out.append(row(k + 1, "+", b[k]))
    return "\n".join(out)
