"""Small terminal UI helpers shared by the CLI.

Keep this module side-effect free: it only formats strings and respects
NO_COLOR / non-interactive output.
"""
from __future__ import annotations

import os
import re
import shutil
import textwrap

from . import security

_X = "\033[0m"
_B = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"

_PALETTE = {
    "bold": _B,
    "info": _CYAN,
    "success": _GREEN,
    "warn": _YELLOW,
    "error": _RED,
    "muted": _DIM,
}

_ICONS = {
    "info": "i",
    "success": "OK",
    "warn": "!",
    "error": "x",
    "tool": ">",
    "result": "<",
}


def _color_enabled(color: bool | None = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if color is not None:
        return color
    return True


def strip_ansi(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _vis_len(s: str) -> int:
    plain = strip_ansi(s)
    try:
        from prompt_toolkit.utils import get_cwidth
        return sum(get_cwidth(ch) for ch in plain)
    except Exception:
        return len(plain)


def paint(s: str, *styles: str, color: bool | None = None) -> str:
    if not _color_enabled(color):
        return s
    prefix = "".join(_PALETTE.get(st, st) for st in styles)
    return f"{prefix}{s}{_X}" if prefix else s


def message(kind: str, text: str, *, color: bool | None = None) -> str:
    """One-line status message with stable severity styling."""
    icon = _ICONS.get(kind, "i")
    return f"{paint(icon, kind, color=color)} {text}"


def stage(title: str, text: str = "", *, color: bool | None = None) -> str:
    """Render a compact process/status line for long-running code work."""
    label = paint(title, "bold", "info", color=color)
    body = f" {paint(text, 'muted', color=color)}" if text else ""
    return f"{paint('◆', 'info', color=color)} {label}{body}"


def kv(rows: list[tuple[str, object]], *, color: bool | None = None, indent: int = 0) -> str:
    """Render aligned key/value rows."""
    if not rows:
        return ""
    width = max(len(str(k)) for k, _ in rows)
    pad = " " * indent
    out = []
    for key, value in rows:
        label = paint(str(key).ljust(width), "muted", color=color)
        out.append(f"{pad}{label}  {value}")
    return "\n".join(out)


def panel(title: str, body: str | list[str], *, kind: str = "info",
          color: bool | None = None, width: int | None = None) -> str:
    """Compact bordered panel that wraps long body lines to terminal width."""
    width = max(42, min(width or shutil.get_terminal_size((88, 24)).columns, 110))
    inner = width - 4
    if isinstance(body, str):
        raw_lines = body.splitlines() or [""]
    else:
        raw_lines = body or [""]
    lines: list[str] = []
    for raw in raw_lines:
        plain = strip_ansi(str(raw))
        if not plain:
            lines.append("")
            continue
        wrapped = textwrap.wrap(plain, width=inner, replace_whitespace=False) or [plain]
        lines.extend(wrapped)

    accent = paint("─", kind, color=color)
    title_text = f" {title} "
    top = paint("╭", kind, color=color) + accent * 2 + paint(title_text, "bold", kind, color=color)
    top += accent * max(0, inner - _vis_len(title_text)) + paint("╮", kind, color=color)
    side = paint("│", kind, color=color)
    bottom = paint("╰", kind, color=color) + accent * (inner + 2) + paint("╯", kind, color=color)
    out = [top]
    for line in lines:
        out.append(f"{side} {line}{' ' * max(0, inner - _vis_len(line))} {side}")
    out.append(bottom)
    return "\n".join(out)


def _short_path(value: str) -> str:
    """审视用的友好路径：cwd 下转相对路径，否则保留文件名优先的短形式。"""
    s = str(value or "")
    try:
        rel = os.path.relpath(s, os.getcwd())
        return rel if not rel.startswith("..") else s
    except (ValueError, OSError):
        return s


def _code_tool_suffix(name: str, args: dict) -> str | None:
    """把写代码/检索类工具调用渲染成人读摘要（而非 repr 截断）。未知工具返回 None。"""
    g = args.get
    if name in ("read_file", "view_file"):
        return f" {_short_path(g('path'))}" if g("path") else None
    if name == "write_file":
        n = len(str(g("content") or ""))
        return f" {_short_path(g('path'))}（{n} 字）" if g("path") else None
    if name == "edit_file":
        return f" {_short_path(g('path'))} · 替换 1 处" if g("path") else None
    if name == "code_apply_patch":
        ops = g("ops") or []
        paths = [_short_path(o.get("path")) for o in ops if isinstance(o, dict) and o.get("path")]
        if not paths:
            return None
        head = paths[0] + (f" 等 {len(paths)} 文件" if len(paths) > 1 else "")
        return f" {head}（{len(paths)} 处补丁）"
    if name in ("grep", "search_code"):
        pat = g("pattern") or g("query") or ""
        scope = g("glob") or g("path") or ""
        return f" '{pat}'" + (f" in {scope}" if scope else "")
    if name in ("run_python", "run_shell", "bash"):
        cmd = str(g("command") or g("code") or "").strip().splitlines()
        return f" {cmd[0][:60]}" if cmd else None
    return None


def tool_call(name: str, args: dict | None = None, *, color: bool | None = None,
              step: str = "") -> str:
    args = args or {}
    suffix = _code_tool_suffix(name, args)
    if suffix is not None:
        suffix = security.redact_text(suffix)
        if len(suffix) > 72:
            suffix = suffix[:69] + "..."
    else:
        pairs = []
        for key, value in args.items():
            text = repr(security.redact_obj(value))
            if len(text) > 60:
                text = text[:57] + "..."
            pairs.append(f"{key}={text}")
        suffix = f"({', '.join(pairs)})" if pairs else "()"
    prefix = f"[{step}] " if step else ""
    return f"{paint(_ICONS['tool'], 'info', color=color)} {paint(prefix, 'muted', color=color)}{paint(name, _B, color=color)}{paint(suffix, 'muted', color=color)}"


def tool_result(text: str, *, color: bool | None = None) -> str:
    if not text:
        return f"  {paint(_ICONS['result'], 'muted', color=color)} {paint('完成，无文本输出', 'muted', color=color)}"
    redacted = security.redact_text(text)
    lines = redacted.splitlines() or [""]
    first = lines[0]
    if len(first) > 120:
        first = first[:117] + "..."
    extra = len([ln for ln in lines[1:] if ln.strip()])
    if extra:
        first = f"{first}  …(+{extra} 行)"
    return f"  {paint(_ICONS['result'], 'muted', color=color)} {paint(first, 'muted', color=color)}"
