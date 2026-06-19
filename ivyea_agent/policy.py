"""Configurable local safety policy for file and command tools."""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

from . import config

POLICY_FILE = config.IVYEA_DIR / "policy.json"

DEFAULT_POLICY: dict[str, Any] = {
    "file_read_roots": [],      # empty = no extra restriction beyond OS permissions
    "file_write_roots": [],
    "command_allow": [],        # glob patterns; empty = allow unless denied
    "command_deny": [
        "git reset --hard*",
        "rm -rf /*",
        "mkfs*",
        "shutdown*",
        "reboot*",
        "dd if=* of=/dev/*",
    ],
    "block_dangerous_commands": True,
}


def load() -> dict[str, Any]:
    data = dict(DEFAULT_POLICY)
    if POLICY_FILE.exists():
        try:
            raw = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except Exception:
            pass
    for key in ("file_read_roots", "file_write_roots", "command_allow", "command_deny"):
        data[key] = [str(x) for x in (data.get(key) or []) if str(x).strip()]
    data["block_dangerous_commands"] = bool(data.get("block_dangerous_commands", True))
    return data


def init(force: bool = False) -> tuple[bool, str]:
    config.ensure_dirs()
    if POLICY_FILE.exists() and not force:
        return False, str(POLICY_FILE)
    POLICY_FILE.write_text(json.dumps(DEFAULT_POLICY, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, str(POLICY_FILE)


def _within(path: Path, roots: list[str]) -> bool:
    if not roots:
        return True
    p = path.resolve()
    for raw in roots:
        root = Path(raw).expanduser().resolve()
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def check_path(path: str | Path, op: str) -> tuple[bool, str]:
    pol = load()
    roots = pol["file_read_roots"] if op == "read" else pol["file_write_roots"]
    p = Path(path).expanduser().resolve()
    if _within(p, roots):
        return True, ""
    return False, f"policy 拒绝 {op} 路径：{p} 不在允许目录内"


def _norm_cmd(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip())


def check_command(command: str) -> tuple[bool, str]:
    pol = load()
    cmd = _norm_cmd(command)
    for pat in pol["command_deny"]:
        if fnmatch.fnmatch(cmd, pat):
            return False, f"policy 拒绝命令（deny={pat}）"
    allow = pol["command_allow"]
    if allow and not any(fnmatch.fnmatch(cmd, pat) for pat in allow):
        return False, "policy 拒绝命令：不匹配 command_allow"
    return True, ""


def render() -> str:
    pol = load()
    lines = ["Ivyea Policy", "", f"- file: {POLICY_FILE}"]
    for key in ("file_read_roots", "file_write_roots", "command_allow", "command_deny"):
        vals = pol.get(key) or []
        lines.append(f"- {key}: " + (", ".join(vals) if vals else "(未限制)"))
    lines.append(f"- block_dangerous_commands: {pol.get('block_dangerous_commands')}")
    return "\n".join(lines)
