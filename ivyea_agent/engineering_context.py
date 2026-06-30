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

CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md", "README.md", "pyproject.toml", "package.json")


def should_include(query: str) -> bool:
    q = (query or "").lower()
    if any(term in q for term in ENGINEERING_TERMS):
        return True
    return bool(re.search(r"\b(test|release|deploy|install|upgrade|bug|fix|refactor|workspace|skill|patch)\b", q))


# 昂贵的 [workspace]+[skills] 段按 (root, git head, clean) 缓存——结构在同一提交/工作树
# 状态下不变，避免每个代码轮都重跑 project_inspect 的文件遍历。[git] 段始终新鲜重算。
_STATIC_CACHE: dict = {}


def _static_sections(root: str | Path) -> list[str]:
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
        rows = skills.status()
        warn = [r for r in rows if not r.get("ok")]
        parts.append("[skills]")
        parts.append(f"active={len(rows)} warnings={len(warn)}")
        if warn:
            parts.append("warnings: " + ", ".join(f"{r['id']}:{','.join(r['issues'][:2])}" for r in warn[:6]))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
        parts.append(f"[skills] unavailable: {e}")
    return parts


def build(root: str | Path = ".", query: str = "", max_chars: int = 2200) -> str:
    if not should_include(query):
        return ""
    try:
        st = git_workflow.status(root)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        st = {}

    key = (str(Path(root).resolve()), st.get("head"), st.get("clean")) if st.get("ok") else None
    if key is not None and key in _STATIC_CACHE:
        parts = list(_STATIC_CACHE[key])
    else:
        parts = _static_sections(root)
        if key is not None:
            if len(_STATIC_CACHE) > 32:
                _STATIC_CACHE.clear()
            _STATIC_CACHE[key] = list(parts)

    if st.get("ok"):   # [git] 段始终新鲜
        changes = st.get("changes") or []
        parts.append("[git]")
        parts.append(f"branch={st.get('branch')} head={st.get('head')} clean={st.get('clean')}")
        if changes:
            parts.append("changes: " + ", ".join(str(c) for c in changes[:10]))

    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n..."
    return text


def repo_conventions(root: str | Path = ".", max_chars: int = 2800) -> dict:
    root_path = Path(root).expanduser().resolve()
    inspected = workspace.project_inspect(root_path)
    files: list[dict[str, str]] = []
    for name in CONVENTION_FILES:
        path = root_path / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        summary = _convention_summary(text, max_chars=max_chars // 2 if name in {"README.md", "pyproject.toml"} else max_chars)
        if summary:
            files.append({"path": name, "summary": summary})
    return {
        "root": str(root_path),
        "files": files,
        "entrypoints": inspected.get("entrypoints", [])[:8],
        "tests": inspected.get("tests", [])[:12],
        "suggested_commands": inspected.get("suggested_commands", [])[:8],
        "risks": inspected.get("risks", []),
    }


def _convention_summary(text: str, max_chars: int = 1400) -> str:
    picked: list[str] = []
    capture = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if capture and picked:
                picked.append("")
            continue
        lower = line.lower()
        if line.startswith("#"):
            capture = any(key in lower for key in (
                "install", "test", "usage", "development", "deploy", "安全", "测试", "开发", "安装", "部署", "约定", "规范",
            ))
        important = capture or any(key in lower for key in (
            "pytest", "ruff", "mypy", "npm test", "cargo test", "go test", "pip install", "python -m", "write", "commit", "push",
        ))
        if important:
            picked.append(line)
        if len("\n".join(picked)) >= max_chars:
            break
    if not picked:
        picked = [line.rstrip() for line in text.splitlines() if line.strip()][:12]
    return "\n".join(picked)[:max_chars].strip()


def _entry_label(entry: dict) -> str:
    value = str(entry.get("path") or "")
    if entry.get("name"):
        value += f":{entry.get('name')}"
    if entry.get("target"):
        value += f"->{entry.get('target')}"
    return value
