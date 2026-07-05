"""Small terminal UI helpers shared by the CLI.

Keep this module side-effect free: it only formats strings and respects
NO_COLOR / non-interactive output.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
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

_ICONS_UNICODE = {
    "info": "·",
    "success": "✓",
    "warn": "▲",
    "error": "✗",
    "tool": "⏺",
    "result": "⎿",
}
_ICONS_ASCII = {
    "info": "i",
    "success": "OK",
    "warn": "!",
    "error": "x",
    "tool": ">",
    "result": "<",
}


def _unicode_glyphs_ok() -> bool:
    """终端能安全显示 Unicode 字形吗？非 UTF-8（如 Windows GBK）回退 ASCII，避免乱码。"""
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in enc


_ICONS = _ICONS_UNICODE if _unicode_glyphs_ok() else _ICONS_ASCII


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


# 工具名 → 友好动词（对标 Claude 的 "Reading 1 file / Running…"，中文 UI 用中文动词）
_TOOL_VERBS = {
    "read_file": "读取文件", "view_file": "读取文件",
    "write_file": "写入文件", "edit_file": "编辑文件",
    "list_dir": "列目录", "glob": "查找文件",
    "grep": "搜索内容", "search_code": "搜索代码", "code_search": "搜索代码",
    "run_command": "执行命令", "run_python": "执行 Python", "run_tests": "运行测试",
    "bash_output": "查看后台输出", "kill_bash": "终止后台任务",
    "web_search": "联网搜索", "web_fetch": "抓取网页",
    "knowledge_search": "查知识库", "skill_search": "查 skill", "recall": "回忆记忆",
    "todo_write": "更新计划",
}


def _tool_detail(name: str, args: dict) -> str | None:
    """工具调用的主要明细（路径/命令/查询），走 └ 连接行展示。"""
    detail = _code_tool_suffix(name, args)
    if detail is not None:
        return detail.strip()
    g = args.get
    if name == "run_command":
        cmd = str(g("command") or g("cmd") or "").strip().splitlines()
        return ("$ " + cmd[0]) if cmd else None
    if name == "list_dir":
        return _short_path(g("path") or ".")
    if name == "glob":
        return str(g("pattern") or g("glob") or "") or None
    if name == "run_tests":
        return _short_path(g("path")) if g("path") else (str(g("target") or "") or "全部")
    if name in ("web_search", "knowledge_search", "skill_search", "recall"):
        return str(g("query") or g("q") or g("pattern") or "") or None
    if name == "web_fetch":
        return str(g("url") or "") or None
    if name == "todo_write":
        todos = g("todos") or []
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        cur = next((t.get("content") for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"), "")
        s = f"{len(todos)} 步 · {done} 完成"
        return s + (f" · 进行中：{cur}" if cur else "")
    return None


def tool_call(name: str, args: dict | None = None, *, color: bool | None = None) -> str:
    args = args or {}
    verb = _TOOL_VERBS.get(name)
    if name == "code_apply_patch":   # 动词带文件数
        ops = args.get("ops") or []
        n = len([o for o in ops if isinstance(o, dict) and o.get("path")])
        verb = f"编辑 {n} 个文件" if n else "应用补丁"
    if verb:
        head = f"{paint(_ICONS['tool'], 'info', color=color)} {paint(verb, _B, color=color)}"
        detail = _tool_detail(name, args)
        if not detail:
            return head
        detail = security.redact_text(detail)
        if len(detail) > 80:
            detail = detail[:77] + "..."
        branch = paint("└", "muted", color=color)
        return f"{head}\n  {branch} {paint(detail, 'muted', color=color)}"
    # 兜底：未列入的工具 → 图标 + 名 + 脱敏参数（保留可读性与安全脱敏）
    pairs = []
    for key, value in args.items():
        text = repr(security.redact_obj(value))
        if len(text) > 60:
            text = text[:57] + "..."
        pairs.append(f"{key}={text}")
    suffix = f"({', '.join(pairs)})" if pairs else "()"
    return f"{paint(_ICONS['tool'], 'info', color=color)} {paint(name, _B, color=color)}{paint(suffix, 'muted', color=color)}"


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
    # 死胡同信号（⚠ 开头，如 grep/glob 扫 0 文件）：黄色 warn 高亮，别被灰掉埋没
    if first.lstrip().startswith("⚠"):
        body = first.lstrip()[1:].lstrip()   # 去 ⚠ 前缀，图标位统一用 warn ▲
        return f"  {paint(_ICONS['warn'], 'warn', color=color)} {paint(body, 'warn', color=color)}"
    return f"  {paint(_ICONS['result'], 'muted', color=color)} {paint(first, 'muted', color=color)}"
