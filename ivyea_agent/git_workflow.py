"""Read-only Git/CI workflow helpers for engineering-agent tasks."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


def _run_git(root: str | Path, args: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(Path(root).expanduser().resolve()),
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        return 1, str(e)
    return proc.returncode, proc.stdout.strip()


def repo_root(path: str | Path = ".") -> Path | None:
    code, out = _run_git(path, ["rev-parse", "--show-toplevel"])
    if code != 0:
        return None
    return Path(out).resolve()


def status(root: str | Path = ".") -> dict[str, Any]:
    r = repo_root(root)
    if not r:
        return {"ok": False, "error": "不是 Git 仓库", "root": str(Path(root).resolve())}
    _, branch = _run_git(r, ["branch", "--show-current"])
    _, head = _run_git(r, ["rev-parse", "--short", "HEAD"])
    _, porcelain = _run_git(r, ["status", "--porcelain=v1", "--branch"])
    _, remote = _run_git(r, ["remote", "-v"])
    changed = [line for line in porcelain.splitlines() if line and not line.startswith("##")]
    return {
        "ok": True,
        "root": str(r),
        "branch": branch or "(detached)",
        "head": head,
        "clean": not changed,
        "changes": changed,
        "remote": remote.splitlines(),
    }


def diff_summary(root: str | Path = ".", staged: bool = False, max_files: int = 40) -> dict[str, Any]:
    r = repo_root(root)
    if not r:
        return {"ok": False, "error": "不是 Git 仓库"}
    args = ["diff", "--stat"] if not staged else ["diff", "--cached", "--stat"]
    _, stat = _run_git(r, args)
    _, names = _run_git(r, ["diff", "--name-status"] if not staged else ["diff", "--cached", "--name-status"])
    files = []
    for line in names.splitlines()[:max_files]:
        parts = line.split("\t", 1)
        if len(parts) == 2:
            files.append({"status": parts[0], "path": parts[1]})
    return {"ok": True, "root": str(r), "staged": staged, "stat": stat, "files": files}


def workflows(root: str | Path = ".") -> list[dict[str, str]]:
    r = repo_root(root) or Path(root).expanduser().resolve()
    wf_dir = r / ".github" / "workflows"
    if not wf_dir.exists():
        return []
    out = []
    for path in sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))):
        text = path.read_text(encoding="utf-8", errors="replace")
        name = ""
        for line in text.splitlines():
            m = re.match(r"\s*name:\s*(.+?)\s*$", line)
            if m:
                name = m.group(1).strip().strip('"').strip("'")
                break
        out.append({"path": path.relative_to(r).as_posix(), "name": name or path.stem})
    return out


def release_plan(version: str, root: str | Path = ".") -> dict[str, Any]:
    r = repo_root(root)
    if not r:
        return {"ok": False, "error": "不是 Git 仓库"}
    st = status(r)
    tag = version if version.startswith("v") else f"v{version}"
    code, _ = _run_git(r, ["rev-parse", "-q", "--verify", f"refs/tags/{tag}"])
    tag_exists = code == 0
    pyproject = r / "pyproject.toml"
    declared = ""
    if pyproject.exists():
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)
        declared = m.group(1) if m else ""
    checks = [
        {"name": "git repository", "ok": True, "detail": str(r)},
        {"name": "working tree clean", "ok": bool(st.get("clean")), "detail": "clean" if st.get("clean") else "has changes"},
        {"name": "version matches pyproject", "ok": declared == tag.lstrip("v"), "detail": declared or "missing"},
        {"name": "tag not exists", "ok": not tag_exists, "detail": tag},
        {"name": "release workflow", "ok": any("release" in w["path"].lower() for w in workflows(r)), "detail": ".github/workflows"},
    ]
    return {"ok": all(c["ok"] for c in checks), "version": tag, "checks": checks}


def render_status(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return f"Git Status\n\n- error: {data.get('error')}"
    lines = [
        "Git Status",
        "",
        f"- root: {data.get('root')}",
        f"- branch: {data.get('branch')}",
        f"- head: {data.get('head')}",
        f"- clean: {data.get('clean')}",
    ]
    if data.get("changes"):
        lines.append("")
        lines.append("Changes:")
        lines.extend(f"- {c}" for c in data["changes"][:40])
    if data.get("remote"):
        lines.append("")
        lines.append("Remotes:")
        lines.extend(f"- {r}" for r in data["remote"][:8])
    return "\n".join(lines)


def render_diff(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return f"Git Diff\n\n- error: {data.get('error')}"
    lines = ["Git Diff", "", f"- root: {data.get('root')}", f"- staged: {data.get('staged')}"]
    if data.get("stat"):
        lines.append("")
        lines.append(data["stat"])
    if data.get("files"):
        lines.append("")
        lines.append("Files:")
        lines.extend(f"- {f['status']} {f['path']}" for f in data["files"])
    return "\n".join(lines)


def render_workflows(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "GitHub Workflows\n\n（未发现 .github/workflows）"
    lines = ["GitHub Workflows", ""]
    lines.extend(f"- {r['path']} · {r['name']}" for r in rows)
    return "\n".join(lines)


def render_release_plan(data: dict[str, Any]) -> str:
    if not data.get("ok") and data.get("error"):
        return f"Release Plan\n\n- error: {data.get('error')}"
    lines = ["Release Plan", "", f"- version: {data.get('version')}", f"- ready: {data.get('ok')}"]
    lines.append("")
    lines.append("Checks:")
    for c in data.get("checks") or []:
        lines.append(f"- {'OK' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")
    return "\n".join(lines)
