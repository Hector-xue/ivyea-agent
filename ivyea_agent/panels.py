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


def render_diff(old: str, new: str, path: str = "", *, color: bool = True, context: int = 2) -> str:
    """old→new 的彩色统一 diff（红删/绿增）。"""
    if old == new:
        return "（无变化）"
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                fromfile=path or "old", tofile=path or "new",
                                lineterm="", n=context)
    out = []
    for ln in diff:
        if ln.startswith(("+++", "---")):
            continue
        if ln.startswith("@@"):
            out.append(f"{_DIM}{ln}{_X}" if color else ln)
        elif ln.startswith("+"):
            out.append(f"{_GREEN}{ln}{_X}" if color else ln)
        elif ln.startswith("-"):
            out.append(f"{_RED}{ln}{_X}" if color else ln)
        else:
            out.append(ln)
    return "\n".join(out)
