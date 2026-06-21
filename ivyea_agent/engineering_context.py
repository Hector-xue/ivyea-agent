"""Compact engineering context injected into chat turns when relevant."""
from __future__ import annotations

import re
from pathlib import Path

from . import git_workflow, skills, workspace

ENGINEERING_TERMS = {
    "代码", "项目", "测试", "单测", "发版", "部署", "安装", "升级", "卸载",
    "github", "git", "ci", "release", "patch", "skill", "workspace",
    "readme", "bug", "修复", "实现", "优化", "重构",
}


def should_include(query: str) -> bool:
    q = (query or "").lower()
    if any(term in q for term in ENGINEERING_TERMS):
        return True
    return bool(re.search(r"\b(test|release|deploy|install|upgrade|bug|fix|refactor|workspace|skill|patch)\b", q))


def build(root: str | Path = ".", query: str = "", max_chars: int = 2200) -> str:
    if not should_include(query):
        return ""
    parts: list[str] = []
    try:
        inspected = workspace.project_inspect(root)
        entrypoints = inspected.get("entrypoints") or []
        tests = inspected.get("tests") or []
        configs = inspected.get("configs") or []
        commands = inspected.get("suggested_commands") or []
        parts.append("[workspace]")
        if entrypoints:
            parts.append("entrypoints: " + ", ".join(_entry_label(e) for e in entrypoints[:6]))
        if tests:
            parts.append("tests: " + ", ".join(str(t) for t in tests[:8]))
        if configs:
            parts.append("configs: " + ", ".join(str(c) for c in configs[:8]))
        if commands:
            parts.append("suggested_commands: " + ", ".join(str(c) for c in commands[:6]))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
        parts.append(f"[workspace] unavailable: {e}")

    try:
        st = git_workflow.status(root)
        if st.get("ok"):
            changes = st.get("changes") or []
            parts.append("[git]")
            parts.append(f"branch={st.get('branch')} head={st.get('head')} clean={st.get('clean')}")
            if changes:
                parts.append("changes: " + ", ".join(str(c) for c in changes[:10]))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
        parts.append(f"[git] unavailable: {e}")

    try:
        rows = skills.status()
        warn = [r for r in rows if not r.get("ok")]
        parts.append("[skills]")
        parts.append(f"active={len(rows)} warnings={len(warn)}")
        if warn:
            parts.append("warnings: " + ", ".join(f"{r['id']}:{','.join(r['issues'][:2])}" for r in warn[:6]))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
        parts.append(f"[skills] unavailable: {e}")

    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n..."
    return text


def _entry_label(entry: dict) -> str:
    value = str(entry.get("path") or "")
    if entry.get("name"):
        value += f":{entry.get('name')}"
    if entry.get("target"):
        value += f"->{entry.get('target')}"
    return value
