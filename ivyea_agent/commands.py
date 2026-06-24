"""用户自定义斜杠命令（对标 Claude Code custom commands）。

放在 ~/.ivyea/commands/<name>.md，聊天里输入 /<name> [参数] 即把文件内容当
prompt 模板发给 agent：模板里的 $ARGUMENTS 替换成命令后输入的文本；没有
$ARGUMENTS 时把参数追加到末尾。纯文本、零依赖、用户可随时增删。
"""
from __future__ import annotations

from pathlib import Path

from . import config

_RESERVED = {"help", "quit", "exit", "plan", "approve", "cost", "compact", "raw",
             "init", "mcp", "knowledge", "skill", "model"}


def commands_dir() -> Path:
    return config.IVYEA_DIR / "commands"


def list_commands() -> dict[str, str]:
    """{name: 首行摘要}。"""
    d = commands_dir()
    if not d.exists():
        return {}
    out: dict[str, str] = {}
    for p in sorted(d.glob("*.md")):
        name = p.stem
        try:
            first = next((ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                          if ln.strip()), "")
        except OSError:
            first = ""
        out[name] = first[:60]
    return out


def expand(name: str, arguments: str = "") -> str | None:
    """返回展开后的 prompt；命令不存在或与内置命令冲突时返回 None。"""
    name = (name or "").strip().lstrip("/")
    if not name or name in _RESERVED:
        return None
    p = commands_dir() / f"{name}.md"
    if not p.is_file():
        return None
    try:
        tmpl = p.read_text(encoding="utf-8")
    except OSError:
        return None
    arguments = arguments or ""
    if "$ARGUMENTS" in tmpl:
        return tmpl.replace("$ARGUMENTS", arguments)
    return tmpl.rstrip() + (("\n\n" + arguments) if arguments.strip() else "")
