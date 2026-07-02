"""Git/CI workflow helpers for engineering-agent tasks."""
from __future__ import annotations

import re
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import policy


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


def _shellish(args: list[str]) -> str:
    return "git " + " ".join(args)


def repo_root(path: str | Path = ".") -> Path | None:
    code, out = _run_git(path, ["rev-parse", "--show-toplevel"])
    if code != 0:
        return None
    return Path(out).resolve()


def checkpoint(root: str | Path = ".") -> dict[str, str] | None:
    """快照当前工作区（tracked 改动）为独立提交对象，**不动索引/工作区/stash 列表/分支历史**。
    返回 {head, stash}（stash="" 表示快照时工作区已干净）；非 git 仓返回 None。供 /rewind 用。"""
    r = repo_root(root)
    if r is None:
        return None
    _, head = _run_git(r, ["rev-parse", "HEAD"])
    code, stash = _run_git(r, ["stash", "create", "ivyea checkpoint"])
    return {"head": head.strip(), "stash": stash.strip() if code == 0 else ""}


def checkpoint_diffstat(cp: dict | None, root: str | Path = ".") -> str:
    """预览：从检查点快照恢复会改动哪些 tracked 文件（git diff --stat）。"""
    r = repo_root(root)
    if r is None or not cp:
        return ""
    target = cp.get("stash") or cp.get("head")
    if not target:
        return ""
    _, out = _run_git(r, ["diff", "--stat", target, "--", "."])
    return out.strip()


def restore_checkpoint(cp: dict | None, root: str | Path = ".") -> tuple[bool, str]:
    """把 tracked 文件恢复到检查点快照（git checkout <ref> -- .）。返回 (ok, 说明)。
    只动 tracked 文件、不碰用户分支历史；非 git 仓返回 (False, ...) 只回退对话。"""
    r = repo_root(root)
    if r is None:
        return False, "不在 git 仓库，未改动文件（仅回退了对话）。"
    if not cp:
        return False, "无检查点。"
    target = cp.get("stash") or cp.get("head")
    if not target:
        return False, "检查点无效。"
    code, out = _run_git(r, ["checkout", target, "--", "."])
    if code != 0:
        return False, f"文件恢复失败：{out[:200]}"
    return True, "已把 tracked 文件恢复到检查点。"


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


def unified_diff(root: str | Path = ".", staged: bool = False, max_lines: int = 500) -> dict[str, Any]:
    """工作区(或 staged)的统一 diff 原文，供 /diff 上色展示。"""
    r = repo_root(root)
    if not r:
        return {"ok": False, "error": "不是 Git 仓库"}
    args = ["diff"] + (["--cached"] if staged else [])
    _, patch = _run_git(r, args)
    lines = patch.splitlines()
    truncated = len(lines) > max_lines
    if truncated:
        patch = "\n".join(lines[:max_lines])
    return {"ok": True, "root": str(r), "staged": staged, "patch": patch, "truncated": truncated}


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


def parse_github_remote(url: str) -> tuple[str, str] | None:
    value = url.strip()
    patterns = [
        r"^https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
        r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
    ]
    for pat in patterns:
        m = re.match(pat, value)
        if m:
            return m.group(1), m.group(2)
    return None


def github_repo(root: str | Path = ".", remote: str = "origin") -> dict[str, Any]:
    r = repo_root(root)
    if not r:
        return {"ok": False, "error": "不是 Git 仓库"}
    code, url = _run_git(r, ["remote", "get-url", remote])
    if code != 0 or not url:
        return {"ok": False, "error": f"未找到 remote：{remote}", "root": str(r)}
    parsed = parse_github_remote(url)
    if not parsed:
        return {"ok": False, "error": f"不是 GitHub remote：{url}", "root": str(r), "remote_url": url}
    owner, repo = parsed
    return {"ok": True, "root": str(r), "remote": remote, "remote_url": url, "owner": owner, "repo": repo, "full_name": f"{owner}/{repo}"}


def _runs_from_gh(full_name: str, limit: int, timeout: int) -> tuple[bool, list[dict[str, Any]], str]:
    try:
        proc = subprocess.run(
            [
                "gh", "run", "list",
                "--repo", full_name,
                "--limit", str(limit),
                "--json", "databaseId,status,conclusion,workflowName,headBranch,headSha,event,createdAt,url",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, [], str(e)
    if proc.returncode != 0:
        return False, [], proc.stdout.strip()
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        return False, [], f"gh 输出不是 JSON：{e}"
    return True, rows if isinstance(rows, list) else [], ""


def _runs_from_api(full_name: str, limit: int, timeout: int) -> tuple[bool, list[dict[str, Any]], str]:
    url = f"https://api.github.com/repos/{full_name}/actions/runs?per_page={max(1, min(limit, 30))}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ivyea-agent",
        },
    )
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        hint = "；私有仓库请先 gh auth login，或设置 GH_TOKEN/GITHUB_TOKEN" if e.code in (401, 403, 404) else ""
        return False, [], f"GitHub API HTTP {e.code}{hint}"
    except (OSError, TimeoutError, json.JSONDecodeError) as e:
        return False, [], str(e)
    rows = []
    for run in payload.get("workflow_runs", [])[:limit]:
        rows.append({
            "databaseId": run.get("id"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "workflowName": run.get("name"),
            "headBranch": run.get("head_branch"),
            "headSha": run.get("head_sha"),
            "event": run.get("event"),
            "createdAt": run.get("created_at"),
            "url": run.get("html_url"),
        })
    return True, rows, ""


def ci_status(root: str | Path = ".", remote: str = "origin", limit: int = 5, timeout: int = 15) -> dict[str, Any]:
    repo = github_repo(root, remote=remote)
    if not repo.get("ok"):
        return {"ok": False, "error": repo.get("error"), "repo": repo}
    full_name = repo["full_name"]
    ok, rows, gh_error = _runs_from_gh(full_name, limit=limit, timeout=timeout)
    if ok:
        return {"ok": True, "repo": repo, "source": "gh", "runs": rows[:limit]}
    ok, rows, api_error = _runs_from_api(full_name, limit=limit, timeout=timeout)
    if ok:
        return {"ok": True, "repo": repo, "source": "github_api", "runs": rows[:limit], "gh_error": gh_error}
    return {
        "ok": False,
        "repo": repo,
        "error": api_error or gh_error or "无法读取 GitHub Actions",
        "gh_error": gh_error,
    }


def _repo_path(root: Path, value: str) -> str | None:
    p = (root / value).resolve()
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return None


def _validate_files(root: Path, files: list[str] | None) -> tuple[bool, list[str], str]:
    if not files:
        return True, [], ""
    safe: list[str] = []
    for raw in files:
        value = str(raw).strip()
        if not value:
            continue
        rel = _repo_path(root, value)
        if rel is None:
            return False, [], f"路径越界：{value}"
        safe.append(rel)
    return True, safe, ""


def write_action(
    action: str,
    root: str | Path = ".",
    *,
    files: list[str] | None = None,
    message: str = "",
    tag: str = "",
    execute: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Prepare or execute a local Git write action.

    Supported actions are intentionally narrow and list-based:
    - stage: git add -- <files> or git add -A
    - commit: git commit -m <message>
    - tag: git tag <tag>
    """
    r = repo_root(root)
    if not r:
        return {"ok": False, "error": "不是 Git 仓库", "action": action}

    if action == "stage":
        ok, safe_files, msg = _validate_files(r, files)
        if not ok:
            return {"ok": False, "error": msg, "action": action, "root": str(r)}
        args = ["add", "--", *safe_files] if safe_files else ["add", "-A"]
        summary = "暂存指定文件" if safe_files else "暂存全部变更"
    elif action == "commit":
        if not message.strip():
            return {"ok": False, "error": "commit 需要 --message", "action": action, "root": str(r)}
        args = ["commit", "-m", message.strip()]
        summary = "创建本地 commit"
    elif action == "tag":
        normalized = tag.strip()
        if not normalized:
            return {"ok": False, "error": "tag 需要 --tag", "action": action, "root": str(r)}
        if not re.match(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$", normalized):
            return {"ok": False, "error": "tag 名称需类似 v1.2.3", "action": action, "root": str(r)}
        code, _ = _run_git(r, ["rev-parse", "-q", "--verify", f"refs/tags/{normalized}"])
        if code == 0:
            return {"ok": False, "error": f"tag 已存在：{normalized}", "action": action, "root": str(r)}
        args = ["tag", normalized]
        summary = "创建本地 tag"
    else:
        return {"ok": False, "error": f"不支持的 Git 写操作：{action}", "action": action, "root": str(r)}

    command = _shellish(args)
    allowed, reason = policy.check_command(command)
    if not allowed:
        return {"ok": False, "error": reason, "action": action, "root": str(r), "command": command}

    result: dict[str, Any] = {
        "ok": True,
        "root": str(r),
        "action": action,
        "summary": summary,
        "command": command,
        "execute": execute,
    }
    if action == "stage":
        result["files"] = safe_files or ["-A"]
    if action == "commit":
        result["message"] = message.strip()
    if action == "tag":
        result["tag"] = tag.strip()

    if not execute:
        result["dry_run"] = True
        return result

    code, out = _run_git(r, args, timeout=timeout)
    result.update({"dry_run": False, "returncode": code, "output": out, "ok": code == 0})
    if code != 0:
        result["error"] = out or "git 命令失败"
    return result


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


def render_ci_status(data: dict[str, Any]) -> str:
    repo = data.get("repo") or {}
    title = "GitHub CI"
    if not data.get("ok"):
        lines = [title, "", f"- repo: {repo.get('full_name', '?')}", f"- error: {data.get('error')}"]
        if data.get("gh_error"):
            lines.append(f"- gh: {data.get('gh_error')}")
        return "\n".join(lines)
    lines = [
        title,
        "",
        f"- repo: {repo.get('full_name')}",
        f"- source: {data.get('source')}",
    ]
    runs = data.get("runs") or []
    if not runs:
        lines.append("")
        lines.append("（未发现 workflow run）")
        return "\n".join(lines)
    lines.append("")
    lines.append("Runs:")
    for run in runs:
        sha = (run.get("headSha") or "")[:7]
        state = run.get("conclusion") or run.get("status") or "unknown"
        lines.append(
            f"- {state} · {run.get('workflowName') or '?'} · {run.get('event') or '?'} · "
            f"{run.get('headBranch') or '?'}@{sha} · {run.get('createdAt') or ''}"
        )
        if run.get("url"):
            lines.append(f"  {run['url']}")
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


def render_write_action(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return f"Git Write\n\n- action: {data.get('action')}\n- error: {data.get('error')}"
    lines = [
        "Git Write",
        "",
        f"- root: {data.get('root')}",
        f"- action: {data.get('action')}",
        f"- summary: {data.get('summary')}",
        f"- command: {data.get('command')}",
        f"- execute: {data.get('execute')}",
        f"- dry_run: {data.get('dry_run', False)}",
    ]
    if data.get("files"):
        lines.append("- files: " + ", ".join(data["files"]))
    if data.get("message"):
        lines.append(f"- message: {data.get('message')}")
    if data.get("tag"):
        lines.append(f"- tag: {data.get('tag')}")
    if data.get("output"):
        lines.append("")
        lines.append(str(data["output"]))
    return "\n".join(lines)
