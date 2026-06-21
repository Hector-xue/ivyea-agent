"""Deterministic code review checks over git diffs."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from . import git_workflow, security

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
]

DANGEROUS_PATTERNS = [
    re.compile(r"shell\s*=\s*True"),
    re.compile(r"rm\s+-rf"),
    re.compile(r"subprocess\.(?:run|Popen|call)\([^)]*shell\s*=\s*True", re.S),
]

BROAD_EXCEPT_PATTERNS = [
    re.compile(r"except\s*:\s*(?:#.*)?$"),
    re.compile(r"except\s+Exception\s*:\s*(?:#.*)?$"),
]

TEST_PREFIXES = ("tests/", "test/", "spec/", "__tests__/")
SOURCE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".php", ".rb"}


def _run(root: str | Path, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(Path(root).expanduser().resolve()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        return 1, str(e)
    return proc.returncode, proc.stdout


def _changed_files(root: str | Path, staged: bool = False) -> list[str]:
    diff = git_workflow.diff_summary(root, staged=staged)
    files = [f["path"] for f in diff.get("files", [])]
    if not staged:
        st = git_workflow.status(root)
        for line in st.get("changes", []):
            if line.startswith("?? "):
                files.append(line[3:])
    return sorted(dict.fromkeys(files))


def _diff(root: str | Path, staged: bool = False) -> str:
    args = ["git", "diff", "--cached", "--unified=80"] if staged else ["git", "diff", "--unified=80"]
    code, out = _run(root, args)
    return out if code == 0 else ""


def _is_source(path: str) -> bool:
    return Path(path).suffix in SOURCE_EXTS and not path.startswith(TEST_PREFIXES)


def _is_test(path: str) -> bool:
    return path.startswith(TEST_PREFIXES) or Path(path).name.startswith("test_")


def review_diff(root: str | Path = ".", staged: bool = False) -> dict[str, Any]:
    repo = git_workflow.repo_root(root)
    if not repo:
        return {"ok": False, "error": "不是 Git 仓库", "findings": []}
    text = _diff(repo, staged=staged)
    files = _changed_files(repo, staged=staged)
    findings: list[dict[str, Any]] = []
    current = ""
    old_line = new_line = 0
    for raw in text.splitlines():
        if raw.startswith("+++ b/"):
            current = raw[6:]
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_line = int(m.group(1)) - 1 if m else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            new_line += 1
            line = raw[1:]
            findings.extend(_line_findings(current, new_line, line))
        elif raw.startswith("-") and not raw.startswith("---"):
            old_line += 1
        else:
            new_line += 1

    if not staged:
        tracked_in_diff = {f.get("path") for f in git_workflow.diff_summary(repo, staged=False).get("files", [])}
        for path in files:
            if path in tracked_in_diff:
                continue
            findings.extend(_review_untracked_file(repo, path))

    source_changed = any(_is_source(p) for p in files)
    tests_changed = any(_is_test(p) for p in files)
    if source_changed and not tests_changed:
        findings.append({
            "severity": "medium",
            "path": "",
            "line": 0,
            "title": "生产代码有改动但未看到测试改动",
            "detail": "建议补充或更新相关测试；如果是纯文档/配置改动，可在说明中解释。",
        })
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f.get("path", ""), int(f.get("line") or 0)))
    return {"ok": True, "root": str(repo), "staged": staged, "files": files, "findings": findings}


def _review_untracked_file(root: Path, path: str) -> list[dict[str, Any]]:
    full = root / path
    if not full.is_file() or full.stat().st_size > 256_000:
        return []
    try:
        text = full.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        out.extend(_line_findings(path, i, line))
    return out


def _line_findings(path: str, line_no: int, line: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    redacted = security.redact_text(line.strip())
    source_line = _is_source(path)
    for pattern in SECRET_PATTERNS:
        if not _is_test(path) and pattern.search(line):
            out.append({
                "severity": "high",
                "path": path,
                "line": line_no,
                "title": "疑似密钥或敏感凭据被写入代码",
                "detail": redacted,
            })
            break
    for pattern in DANGEROUS_PATTERNS:
        if source_line and pattern.search(line):
            out.append({
                "severity": "high",
                "path": path,
                "line": line_no,
                "title": "新增危险命令执行模式",
                "detail": redacted,
            })
            break
    for pattern in BROAD_EXCEPT_PATTERNS:
        if source_line and pattern.search(line):
            out.append({
                "severity": "medium",
                "path": path,
                "line": line_no,
                "title": "新增过宽异常捕获",
                "detail": "请捕获具体异常，或至少记录错误并解释为什么可以吞掉。",
            })
            break
    if ("TODO" in line or "FIXME" in line) and "TODO/FIXME" not in line:
        out.append({
            "severity": "low",
            "path": path,
            "line": line_no,
            "title": "新增 TODO/FIXME",
            "detail": redacted,
        })
    return out


def render(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"Code Review\n\n- error: {result.get('error')}"
    findings = result.get("findings") or []
    lines = [
        "Code Review",
        "",
        f"- root: {result.get('root')}",
        f"- staged: {result.get('staged')}",
        f"- files: {len(result.get('files') or [])}",
        f"- findings: {len(findings)}",
    ]
    if not findings:
        lines.append("")
        lines.append("未发现确定性规则命中的问题。")
        return "\n".join(lines)
    lines.append("")
    for f in findings:
        loc = f"{f.get('path')}:{f.get('line')}" if f.get("path") and f.get("line") else (f.get("path") or "global")
        lines.append(f"- [{f.get('severity')}] {loc} · {f.get('title')}")
        if f.get("detail"):
            lines.append(f"  {f.get('detail')}")
    return "\n".join(lines)
