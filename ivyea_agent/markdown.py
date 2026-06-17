"""极简终端 Markdown → ANSI 渲染（无第三方依赖）。

支持：标题(#~###)、**粗体**、*斜体*/_斜体_、`行内码`、```代码块```、
- / * / 1. 列表、> 引用、--- 分隔线、| 表格 |。设计目标是把 LLM 的 markdown
回复在终端里渲染得清爽可扫读，不追求完整 CommonMark。
"""
from __future__ import annotations

import re

_B, _DIM, _IT, _UND, _X = "\033[1m", "\033[2m", "\033[3m", "\033[4m", "\033[0m"
_CY, _GR, _YE, _MG = "\033[36m", "\033[32m", "\033[33m", "\033[35m"

_CODE_BG = "\033[48;5;236m\033[37m"  # 灰底浅字


def _inline(s: str) -> str:
    """行内：`code` → **bold** → *italic* → _italic_。先抽出 code 占位避免内部被再处理。"""
    holds: list[str] = []

    def _stash(m):
        holds.append(f"{_CODE_BG} {m.group(1)} {_X}")
        return f"\x00{len(holds)-1}\x00"

    s = re.sub(r"`([^`]+)`", _stash, s)
    s = re.sub(r"\*\*([^*]+)\*\*", lambda m: f"{_B}{m.group(1)}{_X}", s)
    s = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", lambda m: f"{_IT}{m.group(1)}{_X}", s)
    s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", lambda m: f"{_IT}{m.group(1)}{_X}", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: f"{_UND}{_CY}{m.group(1)}{_X} ({_DIM}{m.group(2)}{_X})", s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: holds[int(m.group(1))], s)
    return s


def _render_table(rows: list[str]) -> list[str]:
    """把连续的 | 行渲染成对齐表格（rows 已是去掉首尾空白的原始行）。"""
    cells = []
    for r in rows:
        parts = [c.strip() for c in r.strip().strip("|").split("|")]
        cells.append(parts)
    # 丢掉 |---|---| 分隔行
    cells = [c for c in cells if not all(re.fullmatch(r":?-{2,}:?", x or "") for x in c)]
    if not cells:
        return []
    ncol = max(len(c) for c in cells)
    widths = [0] * ncol
    for c in cells:
        for i in range(ncol):
            v = c[i] if i < len(c) else ""
            widths[i] = max(widths[i], _vis_len(v))
    out = []
    for ri, c in enumerate(cells):
        line = []
        for i in range(ncol):
            v = c[i] if i < len(c) else ""
            pad = " " * max(0, widths[i] - _vis_len(v))
            cell = _inline(v)
            line.append(f"{_B}{cell}{_X}{pad}" if ri == 0 else f"{cell}{pad}")
        out.append("  " + " │ ".join(line))
        if ri == 0:
            out.append("  " + "─┼─".join("─" * widths[i] for i in range(ncol)))
    return out


def _vis_len(s: str) -> int:
    try:
        from prompt_toolkit.utils import get_cwidth
        return sum(get_cwidth(ch) for ch in re.sub(r"\033\[[0-9;]*m", "", s))
    except Exception:
        return len(re.sub(r"\033\[[0-9;]*m", "", s))


def _strip_ansi(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)


def render(md: str) -> str:
    """把 markdown 文本渲染成带 ANSI 的终端字符串。NO_COLOR 环境变量则去色。"""
    import os
    out = _render(md)
    return _strip_ansi(out) if os.environ.get("NO_COLOR") else out


def _render(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    table_buf: list[str] = []

    def _flush_table():
        if table_buf:
            out.extend(_render_table(table_buf))
            table_buf.clear()

    for ln in lines:
        fence = re.match(r"^\s*```(\w*)", ln)
        if fence:
            _flush_table()
            in_code = not in_code
            continue
        if in_code:
            out.append(f"{_CODE_BG}  {ln:<60}{_X}")
            continue
        if "|" in ln and ln.strip().startswith("|"):
            table_buf.append(ln)
            continue
        _flush_table()
        h = re.match(r"^(#{1,6})\s+(.*)", ln)
        if h:
            level = len(h.group(1))
            color = (_CY, _MG, _YE, _GR, _GR, _GR)[min(level - 1, 5)]
            prefix = "" if level == 1 else _DIM + "#" * level + " " + _X
            out.append(f"{prefix}{_B}{color}{_inline(h.group(2))}{_X}")
            continue
        if re.match(r"^\s*([-*_])\1{2,}\s*$", ln):
            out.append(f"{_DIM}{'─' * 48}{_X}")
            continue
        q = re.match(r"^\s*>\s?(.*)", ln)
        if q:
            out.append(f"{_DIM}│ {_inline(q.group(1))}{_X}")
            continue
        b = re.match(r"^(\s*)([-*+])\s+(.*)", ln)
        if b:
            out.append(f"{b.group(1)}{_CY}•{_X} {_inline(b.group(3))}")
            continue
        n = re.match(r"^(\s*)(\d+)\.\s+(.*)", ln)
        if n:
            out.append(f"{n.group(1)}{_CY}{n.group(2)}.{_X} {_inline(n.group(3))}")
            continue
        out.append(_inline(ln))
    _flush_table()
    return "\n".join(out)
