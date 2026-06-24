"""Structured patch planning and safe application.

The patch format is intentionally small and deterministic:

{
  "ops": [
    {"path": "relative/file.py", "old": "exact text", "new": "replacement text"}
  ]
}

Each ``old`` block must occur exactly once. This avoids fuzzy edits and keeps
the first writable patch workflow auditable.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from . import panels, policy


def load_spec(path: str | Path) -> dict[str, Any]:
    p = Path(path).expanduser()
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        raise ValueError("patch spec 必须是包含 ops 数组的 JSON 对象")
    return data


def make_spec(path: str, old: str, new: str) -> dict[str, Any]:
    return {"ops": [{"path": path, "old": old, "new": new}]}


def validate_spec(spec: dict[str, Any], root: str | Path = ".") -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    results = []
    ok_all = True
    for i, op in enumerate(spec.get("ops") or [], start=1):
        path = str(op.get("path") or "")
        old = str(op.get("old") or "")
        new = str(op.get("new") or "")
        full = (root_path / path).resolve()
        ok, msg = policy.check_path(full, "write")
        item = {"index": i, "path": path, "ok": True, "message": "", "diff": ""}
        if not path or not old:
            item.update(ok=False, message="path/old 不能为空")
        elif not ok:
            item.update(ok=False, message=msg)
        elif not full.exists() or not full.is_file():
            item.update(ok=False, message=f"文件不存在：{full}")
        else:
            text = full.read_text(encoding="utf-8")
            count = text.count(old)
            if count != 1:
                item.update(ok=False, message=f"old 匹配次数必须为 1，实际 {count}")
            else:
                item["diff"] = panels.render_diff(text, text.replace(old, new, 1), path)
        ok_all = ok_all and bool(item["ok"])
        results.append(item)
    if not results:
        ok_all = False
    return {"ok": ok_all, "root": str(root_path), "ops": results}


def apply_spec(spec: dict[str, Any], root: str | Path = ".", *, execute: bool = False) -> dict[str, Any]:
    validation = validate_spec(spec, root)
    if not validation["ok"]:
        return {"ok": False, "applied": False, "validation": validation, "message": "patch 校验失败"}
    if not execute:
        return {"ok": True, "applied": False, "validation": validation, "message": "dry-run，仅预览未写入"}
    root_path = Path(root).expanduser().resolve()
    touched = []
    for op in spec["ops"]:
        full = (root_path / str(op["path"])).resolve()
        text = full.read_text(encoding="utf-8")
        full.write_text(text.replace(str(op["old"]), str(op.get("new") or ""), 1), encoding="utf-8")
        touched.append(str(op["path"]))
    return {"ok": True, "applied": True, "validation": validation, "touched": touched, "message": "patch 已写入"}


def suggested_tests(root: str | Path = ".") -> list[str]:
    """Suggest local test commands from changed files."""
    root_path = Path(root).expanduser().resolve()
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=str(root_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ["python -m pytest"]
    files = [line[3:] for line in proc.stdout.splitlines() if len(line) > 3]
    tests = sorted(f for f in files if f.startswith("tests/") and f.endswith(".py"))
    if tests:
        return ["python -m pytest " + " ".join(tests[:12])]
    if any(f.startswith("ivyea_agent/") and f.endswith(".py") for f in files):
        return ["python -m pytest", "python -m ivyea_agent.cli codereview --root ."]
    return ["python -m pytest"]


def run_test_command(command: str, root: str | Path = ".", timeout: int = 120) -> dict[str, Any]:
    # Windows has no bash; on POSIX use a non-login shell so the inherited PATH
    # (e.g. the active interpreter from CI's setup-python) is preserved instead
    # of being reset by login profiles.
    shell = ["cmd", "/c", command] if os.name == "nt" else ["bash", "-c", command]
    proc = subprocess.run(
        shell,
        cwd=str(Path(root).expanduser().resolve()),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    out = proc.stdout or ""
    if len(out) > 6000:
        out = out[:6000] + f"\n…（已截断，共 {len(proc.stdout)} 字）"
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "command": command, "output": out}


def render_validation(result: dict[str, Any]) -> str:
    lines = ["Patch Validation", "", f"- root: {result.get('root')}", f"- ok: {result.get('ok')}"]
    for op in result.get("ops") or []:
        lines.append("")
        lines.append(f"## #{op['index']} {op['path']}")
        lines.append(f"- ok: {op['ok']}")
        if op.get("message"):
            lines.append(f"- message: {op['message']}")
        if op.get("diff"):
            lines.extend(["", op["diff"]])
    return "\n".join(lines)


def render_apply(result: dict[str, Any]) -> str:
    lines = ["Patch Apply", "", f"- ok: {result.get('ok')}", f"- applied: {result.get('applied')}", f"- message: {result.get('message')}"]
    if result.get("touched"):
        lines.append("- touched: " + ", ".join(result["touched"]))
    lines.extend(["", render_validation(result.get("validation") or {})])
    return "\n".join(lines)


def render_tests(commands: list[str]) -> str:
    return "Suggested Tests\n\n" + "\n".join(f"- `{cmd}`" for cmd in commands)


def render_test_result(result: dict[str, Any]) -> str:
    return (
        "Patch Test\n\n"
        f"- command: {result.get('command')}\n"
        f"- ok: {result.get('ok')}\n"
        f"- returncode: {result.get('returncode')}\n\n"
        "```text\n"
        f"{result.get('output', '')}\n"
        "```"
    )
