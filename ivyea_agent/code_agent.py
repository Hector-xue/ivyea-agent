"""Deterministic code-agent workflow helpers.

This module gives the CLI a local engineering loop before any model is called:
understand the repo, collect relevant files, suggest a patch/test sequence,
parse test failures, and prepare review gates.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import code_review, config, engineering_context, git_workflow, patcher, workspace

CODE_RUN_DIR = config.IVYEA_DIR / "code-runs"


def _terms(text: str, limit: int = 10) -> list[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text or "")
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        key = word.lower()
        if key not in seen:
            seen.add(key)
            out.append(word)
        if len(out) >= limit:
            break
    return out


def _merge_paths(*groups: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            if path and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _search_paths(goal: str, root: str | Path, limit: int = 12) -> list[str]:
    rows = workspace.search(goal, root=root, limit=limit)
    paths = [r.get("path", "") for r in rows]
    for term in _terms(goal):
        if len(paths) >= limit:
            break
        for row in workspace.search(term, root=root, limit=4):
            path = row.get("path", "")
            if path and path not in paths:
                paths.append(path)
    return paths[:limit]


def task_plan(goal: str, root: str | Path = ".") -> dict[str, Any]:
    root_path = workspace.resolve_root(root)
    idx = workspace.build_index(root_path)
    workspace.save_index(idx)
    inspected = workspace.project_inspect(root_path)
    conventions = engineering_context.repo_conventions(root_path)
    graph = workspace.dependency_graph(root_path, limit=12)
    relevant = _search_paths(goal, root_path)
    entrypoints = [e.get("path", "") for e in inspected.get("entrypoints", []) if e.get("path")]
    tests = inspected.get("tests", [])[:8]
    relevant_files = _merge_paths(relevant, entrypoints[:4], tests[:4])[:18]
    commands = patcher.suggested_tests(root_path)
    if inspected.get("suggested_commands"):
        commands = _merge_paths(commands, inspected["suggested_commands"])[:6]
    return {
        "goal": goal,
        "root": str(root_path),
        "relevant_files": relevant_files,
        "entrypoints": inspected.get("entrypoints", [])[:8],
        "test_files": tests,
        "conventions": conventions,
        "hubs": graph.get("hubs", [])[:8],
        "risks": inspected.get("risks", []),
        "steps": [
            "用 workspace explain 阅读 relevant_files 中的入口、共享模块和测试。",
            "先写最小变更方案；涉及多文件时优先保持现有接口兼容。",
            "生成结构化 patch spec，并用 ivyea patch validate 做 dry-run 校验。",
            "确认后 apply，再运行 suggested_tests。",
            "测试失败时使用 ivyea code repair 解析失败并收敛到下一轮 patch。",
            "最后运行 ivyea code review，确认 diff、测试和风险说明齐全。",
        ],
        "suggested_tests": commands,
        "write_policy": "默认只规划和 dry-run；写文件、提交、推送、发版都需要显式执行参数或人工确认。",
    }


def context(goal: str, root: str | Path = ".", limit: int = 8) -> dict[str, Any]:
    root_path = workspace.resolve_root(root)
    plan = task_plan(goal, root_path)
    files: list[dict[str, Any]] = []
    idx = workspace.ensure_index(root_path)
    by_path = {f.get("path", ""): f for f in idx.get("files", [])}
    for path in plan["relevant_files"][:limit]:
        entry = by_path.get(path)
        if not entry:
            continue
        files.append({
            "path": path,
            "language": entry.get("language"),
            "lines": entry.get("lines"),
            "definitions": entry.get("definitions", [])[:12],
            "imports": entry.get("imports", [])[:16],
            "calls": entry.get("calls", [])[:24],
            "preview": entry.get("preview", ""),
        })
    return {
        "goal": goal,
        "root": str(root_path),
        "files": files,
        "conventions": plan["conventions"],
        "suggested_tests": plan["suggested_tests"],
        "risks": plan["risks"],
    }


def brief(goal: str, root: str | Path = ".", *, budget: int = 6000) -> dict[str, Any]:
    ctx = context(goal, root, limit=10)
    budget = max(1000, budget)
    sections: list[dict[str, str]] = []

    overview = [
        f"goal: {ctx.get('goal')}",
        f"root: {ctx.get('root')}",
        "suggested_tests: " + ", ".join(ctx.get("suggested_tests") or []),
    ]
    if ctx.get("risks"):
        overview.append("risks: " + ", ".join(ctx["risks"]))
    sections.append({"name": "overview", "content": "\n".join(overview)})

    conventions = ctx.get("conventions") or {}
    if conventions.get("files"):
        content = []
        for item in conventions["files"][:3]:
            content.append(f"## {item.get('path')}\n{item.get('summary', '')[:900]}")
        sections.append({"name": "repo_conventions", "content": "\n\n".join(content)})

    for item in ctx.get("files") or []:
        definitions = ", ".join(f"{d.get('qualname')}:{d.get('lineno')}" for d in (item.get("definitions") or [])[:10])
        imports = ", ".join((item.get("imports") or [])[:12])
        calls = ", ".join((item.get("calls") or [])[:16])
        content = "\n".join([
            f"path: {item.get('path')}",
            f"language: {item.get('language')} lines: {item.get('lines')}",
            f"definitions: {definitions}",
            f"imports: {imports}",
            f"calls: {calls}",
            "preview:",
            str(item.get("preview") or "")[:1200],
        ]).strip()
        sections.append({"name": f"file:{item.get('path')}", "content": content})

    selected: list[dict[str, str]] = []
    used = 0
    for section in sections:
        room = budget - used
        if room <= 0:
            break
        content = section["content"]
        if len(content) > room:
            content = content[: max(0, room - 20)].rstrip() + "\n..."
        selected.append({"name": section["name"], "content": content})
        used += len(content)
    return {"goal": goal, "root": ctx.get("root"), "budget": budget, "used": used, "sections": selected}


def quality(root: str | Path = ".") -> dict[str, Any]:
    idx = workspace.ensure_index(root)
    inspected = workspace.project_inspect(root)
    graph = workspace.dependency_graph(root, limit=30)
    files = idx.get("files", [])
    large_files = [
        {"path": f.get("path"), "lines": f.get("lines", 0)}
        for f in files
        if int(f.get("lines") or 0) >= 700
    ]
    long_definitions: list[dict[str, Any]] = []
    for f in files:
        for definition in f.get("definitions", []) or []:
            span = int(definition.get("end_lineno") or 0) - int(definition.get("lineno") or 0) + 1
            if span >= 80:
                long_definitions.append({
                    "path": f.get("path"),
                    "qualname": definition.get("qualname"),
                    "kind": definition.get("kind"),
                    "lineno": definition.get("lineno"),
                    "lines": span,
                })
    tests = set(inspected.get("tests") or [])
    test_names = {Path(t).stem.replace("test_", "") for t in tests}
    source_without_obvious_tests = []
    for f in files:
        path = str(f.get("path") or "")
        name = Path(path).stem
        if not path.endswith(".py") or path.startswith("tests/") or "/tests/" in path or name == "__init__":
            continue
        if name not in test_names and len(source_without_obvious_tests) < 30:
            source_without_obvious_tests.append(path)
    findings: list[dict[str, Any]] = []
    for item in sorted(large_files, key=lambda x: -int(x["lines"]))[:12]:
        findings.append({"severity": "medium", "title": "大文件需要拆分或重点审查", "path": item["path"], "detail": f"{item['lines']} lines"})
    for item in sorted(long_definitions, key=lambda x: -int(x["lines"]))[:12]:
        findings.append({"severity": "medium", "title": "长函数/长方法", "path": item["path"], "line": item["lineno"], "detail": f"{item['qualname']} spans {item['lines']} lines"})
    for hub in graph.get("hubs", [])[:8]:
        if int(hub.get("inbound") or 0) + int(hub.get("outbound") or 0) >= 5:
            findings.append({"severity": "low", "title": "高连接度模块，修改需跑回归", "path": hub.get("path"), "detail": f"in={hub.get('inbound')} out={hub.get('outbound')}"})
    if source_without_obvious_tests:
        findings.append({
            "severity": "low",
            "title": "部分 Python 源文件未发现同名测试",
            "path": "",
            "detail": ", ".join(source_without_obvious_tests[:12]),
        })
    return {
        "root": idx.get("root"),
        "file_count": len(files),
        "test_count": len(tests),
        "large_files": sorted(large_files, key=lambda x: -int(x["lines"]))[:20],
        "long_definitions": sorted(long_definitions, key=lambda x: -int(x["lines"]))[:20],
        "hubs": graph.get("hubs", [])[:12],
        "findings": findings,
    }


def diff_brief(root: str | Path = ".", *, staged: bool = False) -> dict[str, Any]:
    root_path = workspace.resolve_root(root)
    status = git_workflow.status(root_path)
    diff = git_workflow.diff_summary(root_path, staged=staged)
    review = code_review.review_diff(root_path, staged=staged)
    tests = patcher.suggested_tests(root_path)
    files = diff.get("files") or []
    categories = {
        "source": [f["path"] for f in files if Path(f.get("path", "")).suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs"} and not _is_test_path(f.get("path", ""))],
        "tests": [f["path"] for f in files if _is_test_path(f.get("path", ""))],
        "docs": [f["path"] for f in files if Path(f.get("path", "")).suffix.lower() in {".md", ".rst", ".txt"}],
        "config": [f["path"] for f in files if Path(f.get("path", "")).name in {"pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Dockerfile"} or f.get("path", "").startswith(".github/")],
    }
    bullets = []
    if categories["source"]:
        bullets.append("Updated source code: " + ", ".join(categories["source"][:8]))
    if categories["tests"]:
        bullets.append("Updated tests: " + ", ".join(categories["tests"][:8]))
    if categories["docs"]:
        bullets.append("Updated docs: " + ", ".join(categories["docs"][:8]))
    if categories["config"]:
        bullets.append("Updated config/release files: " + ", ".join(categories["config"][:8]))
    if not bullets and files:
        bullets.append("Updated files: " + ", ".join(f["path"] for f in files[:8]))
    findings = review.get("findings") or []
    pr_body = _pr_body(bullets, tests, findings)
    return {
        "root": str(root_path),
        "staged": staged,
        "status": status,
        "diff": diff,
        "categories": categories,
        "bullets": bullets,
        "review": review,
        "suggested_tests": tests,
        "pr_body": pr_body,
    }


def release_check(root: str | Path = ".", *, version: str = "") -> dict[str, Any]:
    root_path = workspace.resolve_root(root)
    status = git_workflow.status(root_path)
    review = review_ready(root_path)
    q = quality(root_path)
    inspected = workspace.project_inspect(root_path)
    detected_version = version or _project_version(root_path)
    release_plan = git_workflow.release_plan(detected_version, root_path) if detected_version else {"ok": False, "error": "未提供版本，且 pyproject.toml 未发现 version"}
    hard_blockers: list[str] = []
    warnings: list[str] = []
    if not status.get("ok"):
        hard_blockers.append(status.get("error", "Git 状态不可用"))
    if status.get("ok") and status.get("clean") is False:
        warnings.append("工作区有未提交改动；发版前需要确认是否全部纳入。")
    if any(f.get("severity") == "high" for f in review.get("review", {}).get("findings", [])):
        hard_blockers.append("code review 存在 high findings")
    if not inspected.get("tests"):
        warnings.append("未发现测试文件")
    if not release_plan.get("ok"):
        warnings.append(str(release_plan.get("error") or "release-plan 未通过"))
    medium_quality = [f for f in q.get("findings", []) if f.get("severity") == "medium"]
    if len(medium_quality) >= 10:
        warnings.append(f"质量扫描 medium findings 较多：{len(medium_quality)}")
    return {
        "root": str(root_path),
        "version": detected_version,
        "ok": not hard_blockers,
        "hard_blockers": hard_blockers,
        "warnings": warnings,
        "status": status,
        "review": review,
        "quality": q,
        "release_plan": release_plan,
        "suggested_tests": patcher.suggested_tests(root_path),
    }


def refs(symbol: str, root: str | Path = ".", *, limit: int = 120) -> dict[str, Any]:
    root_path = workspace.resolve_root(root)
    idx = workspace.ensure_index(root_path)
    symbol = (symbol or "").strip()
    if not symbol:
        return {"root": str(root_path), "symbol": symbol, "matches": []}
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    matches: list[dict[str, Any]] = []
    for entry in idx.get("files", []):
        path = str(entry.get("path") or "")
        for definition in entry.get("definitions", []) or []:
            if symbol in {definition.get("name"), definition.get("qualname")}:
                matches.append({"path": path, "line": definition.get("lineno", 0), "kind": "definition", "text": str(definition.get("qualname") or symbol)})
        for raw in entry.get("imports", []) or []:
            if symbol in str(raw).split(".") or symbol == str(raw):
                matches.append({"path": path, "line": 0, "kind": "import", "text": str(raw)})
        for call in entry.get("calls", []) or []:
            call_s = str(call)
            if call_s == symbol or call_s.endswith("." + symbol):
                matches.append({"path": path, "line": 0, "kind": "call", "text": call_s})
        full = root_path / path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                item = {"path": path, "line": line_no, "kind": "text", "text": line.strip()[:220]}
                if item not in matches:
                    matches.append(item)
            if len(matches) >= limit:
                return {"root": str(root_path), "symbol": symbol, "matches": matches[:limit]}
    return {"root": str(root_path), "symbol": symbol, "matches": matches[:limit]}


def rename_plan(symbol: str, new_name: str, root: str | Path = ".", *, limit: int = 80) -> dict[str, Any]:
    root_path = workspace.resolve_root(root)
    symbol = (symbol or "").strip()
    new_name = (new_name or "").strip()
    ref_data = refs(symbol, root_path, limit=limit)
    spec = {"ops": []}
    warnings: list[str] = []
    if not symbol or not new_name:
        warnings.append("symbol 和 new_name 不能为空")
    elif not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", symbol) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", new_name):
        warnings.append("当前 rename-plan 只支持简单标识符")
    else:
        touched: set[str] = set()
        for item in ref_data.get("matches", []):
            path = item.get("path", "")
            if not path or path in touched:
                continue
            full = root_path / path
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            new_text, count = re.subn(rf"\b{re.escape(symbol)}\b", new_name, text)
            if count:
                spec["ops"].append({"path": path, "old": text, "new": new_text})
                touched.add(path)
            if len(spec["ops"]) >= limit:
                break
    validation = patcher.validate_spec(spec, root=root_path) if spec["ops"] else None
    if validation and not validation.get("ok"):
        warnings.append("生成的 patch spec 未通过 validate；请检查 old 是否唯一匹配或文件是否可写。")
    return {
        "root": str(root_path),
        "symbol": symbol,
        "new_name": new_name,
        "matches": ref_data.get("matches", []),
        "spec": spec,
        "validation": validation,
        "warnings": warnings,
        "suggested_tests": _merge_paths(patcher.suggested_tests(root_path), workspace.impact_analysis(symbol, root_path).get("suggested_tests", []))[:6],
        "instructions": [
            "rename-plan 只生成 patch spec 草案，不写文件。",
            "先人工检查 spec，确认没有改到字符串、文档或不该改的公共 API。",
            "运行 ivyea patch validate rename.json --root .，通过后再显式 apply --execute。",
        ],
    }


def _is_test_path(path: str) -> bool:
    low = (path or "").lower()
    name = Path(path).name
    return low.startswith("tests/") or "/tests/" in low or name.startswith("test_") or name.endswith((".test.ts", ".spec.ts", ".test.js", ".spec.js"))


def _pr_body(bullets: list[str], tests: list[str], findings: list[dict[str, Any]]) -> str:
    lines = ["## Summary"]
    lines.extend(f"- {b}" for b in (bullets or ["No changed files detected."]))
    lines.extend(["", "## Validation"])
    lines.extend(f"- [ ] `{cmd}`" for cmd in tests)
    lines.extend(["", "## Risk"])
    if findings:
        lines.append(f"- Review findings: {len(findings)}")
        for item in findings[:6]:
            loc = item.get("path") or "global"
            lines.append(f"- {item.get('severity')}: {loc} - {item.get('title')}")
    else:
        lines.append("- Deterministic review found no issues.")
    return "\n".join(lines)


def _project_version(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return ""
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"(?m)^\s*version\s*=\s*['\"]([^'\"]+)['\"]", text)
    return f"v{match.group(1)}" if match else ""


def parse_pytest_output(text: str) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in (text or "").splitlines():
        failed = re.match(r"(?:FAILED|ERROR)\s+(.+?)(?:\s+-\s+(.+))?$", line.strip())
        if failed:
            nodeid = failed.group(1)
            current = _ensure_failure(failures, nodeid)
            current["reason"] = failed.group(2) or current.get("reason", "")
            _set_exception(current, current.get("reason", ""))
            continue
        section = re.match(r"_{2,}\s+(.+?)\s+_{2,}$", line.strip())
        if section:
            title = section.group(1).strip()
            current = _ensure_failure(failures, title)
            continue
        frame = re.match(r"(?P<path>[\w./\\-]+\.py):(?P<line>\d+):\s+in\s+(?P<func>[\w_<>.-]+)", line.strip())
        if frame and current is not None:
            current.setdefault("frames", []).append({
                "path": frame.group("path").replace("\\", "/"),
                "line": int(frame.group("line")),
                "function": frame.group("func"),
            })
            continue
        source = re.match(r">\s+(?P<code>.+)$", line)
        if source and current is not None:
            current["source"] = source.group("code").rstrip()
            continue
        if failures and (line.startswith("E   ") or "AssertionError" in line or "Traceback" in line):
            target = current or failures[-1]
            target["errors"].append(line.strip())
            _set_exception(target, line.strip())
    return {"failure_count": len(failures), "failures": failures[:20]}


def _ensure_failure(failures: list[dict[str, Any]], nodeid: str) -> dict[str, Any]:
    nodeid = re.sub(r"^ERROR\s+collecting\s+", "", nodeid).strip()
    for failure in failures:
        if failure.get("nodeid") == nodeid:
            return failure
        if "::" in nodeid and failure.get("nodeid") == nodeid.rsplit("::", 1)[-1]:
            failure["nodeid"] = nodeid
            failure["path"] = nodeid.split("::", 1)[0]
            failure["test"] = "::".join(nodeid.split("::")[1:])
            return failure
    item = {
        "nodeid": nodeid,
        "path": nodeid.split("::", 1)[0],
        "test": "::".join(nodeid.split("::")[1:]),
        "reason": "",
        "exception_type": "",
        "exception": "",
        "source": "",
        "frames": [],
        "errors": [],
    }
    failures.append(item)
    return item


def _set_exception(failure: dict[str, Any], line: str) -> None:
    text = (line or "").strip()
    if text.startswith("E   "):
        text = text[4:].strip()
    match = re.match(r"([A-Za-z_][\w.]*Error|[A-Za-z_][\w.]*Exception|AssertionError)(?::\s*(.*))?", text)
    if not match:
        return
    failure["exception_type"] = match.group(1)
    failure["exception"] = match.group(2) or text


def _failure_kind(failure: dict[str, Any]) -> str:
    text = " ".join([
        failure.get("exception_type", ""),
        failure.get("exception", ""),
        failure.get("reason", ""),
        failure.get("source", ""),
        " ".join(failure.get("errors", [])[:5]),
    ]).lower()
    exc = (failure.get("exception_type") or "").lower()
    if "syntaxerror" in exc or "indentationerror" in exc:
        return "syntax"
    if "modulenotfounderror" in exc or "importerror" in exc or "cannot import name" in text:
        return "import"
    if "assertionerror" in exc or "assert " in text or "assertionerror" in text:
        return "assertion"
    if "typeerror" in exc:
        return "type"
    if "keyerror" in exc or "indexerror" in exc:
        return "data-shape"
    if "filenotfounderror" in exc:
        return "missing-file"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "permissionerror" in exc:
        return "permission"
    return "unknown"


def _kind_action(kind: str) -> str:
    actions = {
        "assertion": "对照失败断言和业务期望，优先修生产代码；只有期望已变化时才调整测试。",
        "import": "检查模块路径、导出符号和可选依赖，不要用宽泛 except 掩盖真实导入失败。",
        "syntax": "先修复语法/缩进，让测试能够收集，再进入行为修复。",
        "type": "沿调用链核对入参、返回值和 None 分支，补最小防御或接口兼容。",
        "data-shape": "核对字典/列表结构和边界输入，补缺省值或上游数据校验。",
        "missing-file": "确认 fixture、路径基准和生成文件流程，避免写死本机绝对路径。",
        "timeout": "定位阻塞 IO、死循环或过宽测试范围，先做最小复现再扩大回归。",
        "permission": "检查写入目录和命令权限，优先改到项目可写路径或显式配置。",
        "unknown": "先保留完整失败输出，按 nodeid、frame、source 三条线索定位最小修改点。",
    }
    return actions.get(kind, actions["unknown"])


def _focused_tests(parsed: dict[str, Any], limit: int = 6) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    for failure in parsed.get("failures") or []:
        nodeid = str(failure.get("nodeid") or "")
        if not nodeid or not nodeid.endswith(".py") and ".py::" not in nodeid:
            continue
        command = "python -m pytest " + shlex.quote(nodeid)
        if command not in seen:
            seen.add(command)
            commands.append(command)
        if len(commands) >= limit:
            break
    return commands


def _failure_summary(parsed: dict[str, Any], likely_files: list[str]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for failure in parsed.get("failures") or []:
        kind = _failure_kind(failure)
        rerun = _focused_tests({"failures": [failure]}, limit=1)
        frame_files = [
            frame.get("path", "")
            for frame in failure.get("frames", []) or []
            if frame.get("path")
        ]
        summaries.append({
            "nodeid": failure.get("nodeid", ""),
            "kind": kind,
            "exception_type": failure.get("exception_type", ""),
            "likely_files": _merge_paths([failure.get("path", "")], frame_files, likely_files)[:6],
            "rerun": rerun[0] if rerun else "",
            "action": _kind_action(kind),
        })
    return summaries


def repair_plan(output: str, root: str | Path = ".") -> dict[str, Any]:
    parsed = parse_pytest_output(output)
    test_files = [f.get("path", "") for f in parsed["failures"] if f.get("path")]
    frame_files: list[str] = []
    terms: list[str] = []
    for failure in parsed["failures"]:
        for frame in failure.get("frames", []) or []:
            path = frame.get("path", "")
            if path and not path.startswith("tests/") and path not in frame_files:
                frame_files.append(path)
        terms.extend(_terms(" ".join([
            failure.get("nodeid", ""),
            failure.get("reason", ""),
            failure.get("exception_type", ""),
            failure.get("source", ""),
            " ".join(failure.get("errors", [])[:3]),
        ]), limit=8))
    related: list[str] = []
    for term in terms[:12]:
        for row in workspace.search(term, root=root, limit=4):
            path = row.get("path", "")
            if path and path not in related:
                related.append(path)
    likely_files = _merge_paths(test_files, frame_files, related)[:18]
    focused = _focused_tests(parsed)
    summaries = _failure_summary(parsed, likely_files)
    return {
        "root": str(workspace.resolve_root(root)),
        "failure_count": parsed["failure_count"],
        "failures": parsed["failures"],
        "failure_summary": summaries,
        "likely_files": likely_files,
        "focused_tests": focused,
        "repair_loop": [
            "读取 failure_summary 中每个 kind 的 action，先处理能让测试继续收集的 syntax/import 问题。",
            "打开 likely_files 前 3-6 个文件，按失败 frame 定位最小修改点。",
            "生成或手写最小 patch 后先运行 focused_tests；不要直接全量回归掩盖定位成本。",
            "focused_tests 通过后运行 suggested_tests 或项目默认测试，失败则把新输出再次交给 ivyea code repair。",
            "最终运行 ivyea code review，输出修改范围、测试命令和剩余风险。",
        ],
        "next_steps": [
            "先打开失败测试，确认断言期望和输入边界。",
            "根据 failure 里的符号名搜索生产代码，定位最小修改点。",
            "补或调整测试后生成 patch spec，先 validate 再 apply。",
            "只重跑失败测试；通过后再跑完整 suggested_tests。",
        ],
    }


def run_tests(command: str = "python -m pytest", root: str | Path = ".", timeout: int = 120) -> dict[str, Any]:
    result = patcher.run_test_command(command, root=root, timeout=timeout)
    result["parsed"] = parse_pytest_output(result.get("output", ""))
    return result


def patch_apply_loop(
    spec: dict[str, Any],
    root: str | Path = ".",
    *,
    test_command: str = "",
    execute: bool = False,
    timeout: int = 120,
    persist: bool = False,
) -> dict[str, Any]:
    """Validate/apply/test one structured patch and produce an audit record."""
    root_path = workspace.resolve_root(root)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = hashlib.sha1(f"{root_path}:{started_at}:patch-loop:{json.dumps(spec, sort_keys=True, ensure_ascii=False)}".encode("utf-8")).hexdigest()[:12]
    validation = patcher.validate_spec(spec, root=root_path)
    apply_result = {"ok": False, "applied": False, "validation": validation, "message": "patch 校验失败"}
    test_result = None
    repair = None
    if validation.get("ok"):
        apply_result = patcher.apply_spec(spec, root=root_path, execute=execute)
        if execute and apply_result.get("ok") and apply_result.get("applied"):
            command = test_command or (patcher.suggested_tests(root_path)[0] if patcher.suggested_tests(root_path) else "python -m pytest")
            test_result = run_tests(command, root=root_path, timeout=timeout)
            if not test_result.get("ok"):
                repair = repair_plan(test_result.get("output", ""), root=root_path)
    review = review_ready(root_path)
    data = {
        "id": run_id,
        "goal": "structured patch apply/test loop",
        "root": str(root_path),
        "started_at": started_at,
        "mode": "execute" if execute else "dry-run",
        "patch": {
            "status": "applied" if apply_result.get("applied") else ("valid" if validation.get("ok") else "invalid"),
            "spec": spec,
            "validation": validation,
            "apply": apply_result,
        },
        "selected_tests": [test_command] if test_command else patcher.suggested_tests(root_path)[:4],
        "test_result": test_result,
        "repair": repair,
        "review": review,
        "rounds": [{
            "round": 1,
            "status": "completed" if (not test_result or test_result.get("ok")) and apply_result.get("ok") else "needs_repair",
            "patch_status": "applied" if apply_result.get("applied") else ("dry_run" if validation.get("ok") else "invalid"),
            "test_status": "not_run" if test_result is None else ("passed" if test_result.get("ok") else "failed"),
            "repair_status": "ready" if repair else "not_needed",
        }],
        "next_steps": [
            "dry-run 模式只预览，不写文件；确认后用 --execute 进入真实写入。",
            "测试失败时读取 repair.failure_summary，生成下一轮最小 patch spec。",
            "通过后运行 code review / diff-brief，记录验证命令和剩余风险。",
        ],
    }
    if persist:
        save_run(data)
    return data


def impact(target: str, root: str | Path = ".") -> dict[str, Any]:
    data = workspace.impact_analysis(target, root=root)
    return {
        **data,
        "next_steps": [
            "先阅读 Definitions 和 Direct Files，确认真实修改点。",
            "检查 Importers/Callers，判断接口变更是否需要兼容层。",
            "优先运行 Suggested Tests；如果没有命中特定测试，跑项目默认测试。",
            "影响公共入口或共享模块时，补充 code review 说明和回归测试。",
        ],
    }


PATCH_SYSTEM = """You are Ivyea Agent's code patch planner.
Return only JSON in this exact shape: {"ops":[{"path":"relative/file","old":"exact existing text","new":"replacement text"}]}.
Rules:
- old must be an exact unique substring from the target file.
- Keep the patch minimal.
- Do not include markdown fences.
- Do not invent files.
- Do not commit, push, install, or run commands.
"""


def llm_patch_request(goal: str, root: str | Path = ".", *, path: str = "") -> dict[str, Any]:
    plan = task_plan(goal, root)
    ctx = context(goal, root, limit=6)
    target = path or _default_patch_path(plan.get("relevant_files", []))
    focus = []
    for item in ctx.get("files") or []:
        if not target or item.get("path") == target or len(focus) < 3:
            focus.append(item)
    user = {
        "goal": goal,
        "root": str(workspace.resolve_root(root)),
        "target_path": target,
        "relevant_files": plan.get("relevant_files", [])[:12],
        "repo_conventions": ctx.get("conventions", {}),
        "files": focus,
        "required_output": {"ops": [{"path": target or "relative/file", "old": "exact existing text", "new": "replacement text"}]},
    }
    return {"system": PATCH_SYSTEM, "user": json.dumps(user, ensure_ascii=False, indent=2), "target_path": target, "plan": plan}


def llm_patch_candidate(
    goal: str,
    root: str | Path = ".",
    *,
    path: str = "",
    provider: Any = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    req = llm_patch_request(goal, root, path=path)
    if provider is None:
        return {
            "goal": goal,
            "root": str(workspace.resolve_root(root)),
            "status": "request_only",
            "request": {"system": req["system"], "user": req["user"]},
            "candidate_files": req["plan"].get("relevant_files", [])[:12],
            "instructions": ["配置模型并加 --call 后才会请求 LLM；默认只输出请求包。"],
        }
    raw = provider.complete(req["system"], req["user"], json_mode=True, temperature=0.1, timeout=timeout)
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        return {
            "goal": goal,
            "root": str(workspace.resolve_root(root)),
            "status": "invalid_json",
            "raw": raw,
            "error": str(e),
            "candidate_files": req["plan"].get("relevant_files", [])[:12],
        }
    validation = patcher.validate_spec(spec, root=root)
    return {
        "goal": goal,
        "root": str(workspace.resolve_root(root)),
        "status": "valid" if validation.get("ok") else "invalid",
        "spec": spec,
        "raw": raw,
        "validation": validation,
        "candidate_files": req["plan"].get("relevant_files", [])[:12],
        "instructions": [
            "这是 LLM 生成的候选 patch，已做 dry-run validate。",
            "只有人工确认后才运行 ivyea patch apply patch.json --root . --execute。",
        ],
    }


def patch_candidate(
    goal: str,
    root: str | Path = ".",
    *,
    path: str = "",
    old: str = "",
    new: str = "",
) -> dict[str, Any]:
    plan = task_plan(goal, root)
    candidate_path = path or _default_patch_path(plan.get("relevant_files", []))
    spec = {"ops": []}
    status = "needs_input"
    validation = None
    if candidate_path and old:
        spec = patcher.make_spec(candidate_path, old, new)
        validation = patcher.validate_spec(spec, root=root)
        status = "valid" if validation.get("ok") else "invalid"
    elif candidate_path:
        spec = {"ops": [{"path": candidate_path, "old": "", "new": ""}]}
    return {
        "goal": goal,
        "root": str(workspace.resolve_root(root)),
        "status": status,
        "spec": spec,
        "validation": validation,
        "candidate_files": plan.get("relevant_files", [])[:12],
        "instructions": [
            "在 spec.ops[0].old 中填入目标文件里唯一匹配的原文片段。",
            "在 spec.ops[0].new 中填入替换后的文本。",
            "运行 ivyea patch validate patch.json --root . 做 dry-run 校验。",
            "只有确认后才运行 ivyea patch apply patch.json --root . --execute。",
        ],
    }


def _default_patch_path(paths: list[str]) -> str:
    for path in paths:
        name = Path(path).name
        low = path.lower()
        if not (low.startswith("tests/") or "/tests/" in low or name.startswith("test_")):
            return path
    return paths[0] if paths else ""


def run_loop(
    goal: str,
    root: str | Path = ".",
    *,
    test_command: str = "",
    run_tests_enabled: bool = False,
    max_rounds: int = 1,
    persist: bool = False,
    llm_patch: bool = False,
    patch_provider: Any = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    root_path = workspace.resolve_root(root)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = hashlib.sha1(f"{root_path}:{goal}:{started_at}".encode("utf-8")).hexdigest()[:12]
    plan = task_plan(goal, root_path)
    ctx = context(goal, root_path, limit=6)
    impact_targets = _impact_targets(plan, ctx)
    impacts = [impact(target, root_path) for target in impact_targets[:4]]
    selected_tests = _merge_paths([test_command] if test_command else [], plan.get("suggested_tests", []))[:4]
    test_result = None
    repair = None
    if run_tests_enabled:
        command = test_command or (selected_tests[0] if selected_tests else "python -m pytest")
        test_result = run_tests(command, root=root_path)
        if not test_result.get("ok"):
            repair = repair_plan(test_result.get("output", ""), root=root_path)
    patch_stage = (
        llm_patch_candidate(goal, root_path, provider=patch_provider, timeout=timeout)
        if llm_patch
        else patch_candidate(goal, root_path)
    )
    rounds = _planned_rounds(max_rounds, patch_stage, test_result, repair)
    review = review_ready(root_path)
    data = {
        "id": run_id,
        "goal": goal,
        "root": str(root_path),
        "started_at": started_at,
        "max_rounds": max(1, max_rounds),
        "mode": "dry-run",
        "plan": plan,
        "context": ctx,
        "impact_targets": impact_targets[:4],
        "impacts": impacts,
        "patch": patch_stage,
        "rounds": rounds,
        "selected_tests": selected_tests,
        "test_result": test_result,
        "repair": repair,
        "review": review,
        "next_steps": [
            "确认 plan/context/impact 命中的文件是否正确。",
            "下一步由 code patch 生成候选 patch spec，并先 validate。",
            "patch 通过 dry-run 后再显式 apply --execute。",
            "运行 selected_tests；失败时用 repair 进入下一轮。",
        ],
    }
    if persist:
        save_run(data)
    return data


def task_bundle(
    goal: str,
    root: str | Path = ".",
    *,
    test_output: str = "",
    limit: int = 8,
) -> dict[str, Any]:
    """Build a read-only product bundle for a multi-round code task."""
    root_path = workspace.resolve_root(root)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    bundle_id = hashlib.sha1(f"{root_path}:{goal}:{generated_at}:bundle".encode("utf-8")).hexdigest()[:12]
    plan = task_plan(goal, root_path)
    ctx = context(goal, root_path, limit=limit)
    q = quality(root_path)
    impact_targets = _impact_targets(plan, ctx)
    impacts = [impact(target, root_path) for target in impact_targets[:4]]
    patch_stage = patch_candidate(goal, root_path)
    selected_tests = _merge_paths(plan.get("suggested_tests", []), q.get("suggested_tests", []) if isinstance(q.get("suggested_tests"), list) else [])[:6]
    if not selected_tests:
        selected_tests = patcher.suggested_tests(root_path)[:6]
    repair = repair_plan(test_output, root_path) if (test_output or "").strip() else None
    review = review_ready(root_path)
    phases = [
        {"name": "plan", "status": "ready", "summary": f"{len(plan.get('relevant_files') or [])} relevant files"},
        {"name": "context", "status": "ready", "summary": f"{len(ctx.get('files') or [])} files packed"},
        {"name": "impact", "status": "ready", "summary": f"{len(impacts)} impact targets"},
        {"name": "patch", "status": patch_stage.get("status", "needs_input"), "summary": "dry-run patch template only"},
        {"name": "test", "status": "selected", "summary": ", ".join(selected_tests[:3])},
        {"name": "repair", "status": "ready" if repair else "waiting_for_failure_output", "summary": f"{repair.get('failure_count')} failures" if repair else "no test output supplied"},
        {"name": "review", "status": "ready" if review.get("ready") else "needs_attention", "summary": f"{len(review.get('review', {}).get('findings') or [])} findings"},
    ]
    resume_prompt = _bundle_resume_prompt(goal, plan, selected_tests, repair)
    return {
        "id": bundle_id,
        "goal": goal,
        "root": str(root_path),
        "generated_at": generated_at,
        "mode": "read-only-task-bundle",
        "plan": plan,
        "context": ctx,
        "quality": q,
        "impact_targets": impact_targets[:4],
        "impacts": impacts,
        "patch": patch_stage,
        "selected_tests": selected_tests,
        "repair": repair,
        "review": review,
        "phases": phases,
        "resume_prompt": resume_prompt,
        "next_steps": [
            "确认 relevant_files 和 impact_targets 是否覆盖真实修改范围。",
            "填写 patch.spec.ops 的 old/new，先运行 patch validate。",
            "显式 apply 后先运行 selected_tests；失败输出交给 code repair 或重新生成 bundle。",
            "通过后运行 code review，输出 diff 摘要、验证命令和剩余风险。",
        ],
    }


def _bundle_resume_prompt(goal: str, plan: dict[str, Any], selected_tests: list[str], repair: dict[str, Any] | None) -> str:
    files = "\n".join(f"- {path}" for path in (plan.get("relevant_files") or [])[:10])
    tests = "\n".join(f"- {cmd}" for cmd in selected_tests[:6])
    lines = [
        "继续 Ivyea 代码任务。不要重复已经完成的 repo 扫描，直接从任务包继续。",
        "",
        f"目标：{goal}",
        "",
        "已识别相关文件：",
        files or "- (none)",
        "",
        "建议测试：",
        tests or "- (none)",
    ]
    if repair:
        lines.extend(["", "上一轮失败摘要："])
        for item in repair.get("failure_summary") or []:
            lines.append(f"- {item.get('nodeid')} kind={item.get('kind')} rerun={item.get('rerun')}")
    lines.extend([
        "",
        "下一步：先生成最小 patch spec 并 validate；需要写文件或执行命令时必须显式审批。",
    ])
    return "\n".join(lines)


def save_run(data: dict[str, Any]) -> Path:
    CODE_RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = CODE_RUN_DIR / f"{data.get('id')}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_run(run_id: str) -> dict[str, Any]:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", run_id or "")
    if not safe:
        raise ValueError("run id 不能为空")
    path = CODE_RUN_DIR / f"{safe}.json"
    if not path.exists():
        raise FileNotFoundError(f"未找到 code run：{safe}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    if not CODE_RUN_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(CODE_RUN_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({
            "id": data.get("id") or path.stem,
            "goal": data.get("goal", ""),
            "root": data.get("root", ""),
            "started_at": data.get("started_at", ""),
            "mode": data.get("mode", ""),
            "patch_status": (data.get("patch") or {}).get("status", ""),
            "review_ready": (data.get("review") or {}).get("ready"),
        })
    return rows


def sandbox_plan(root: str | Path = ".", *, name: str = "") -> dict[str, Any]:
    root_path = workspace.resolve_root(root)
    repo = git_workflow.repo_root(root_path)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "-", name or "ivyea-code-sandbox").strip("-") or "ivyea-code-sandbox"
    if repo:
        target = repo.parent / f"{repo.name}-{safe_name}"
        method = "git-worktree"
        commands = [
            f"git worktree add {target} HEAD",
            f"cd {target}",
            "ivyea code run \"<goal>\" --root .",
            f"git worktree remove {target}",
        ]
    else:
        target = Path("/tmp") / safe_name
        method = "directory-copy"
        commands = [
            f"mkdir -p {target}",
            f"rsync -a --exclude .git --exclude .venv --exclude node_modules {root_path}/ {target}/",
            f"cd {target}",
            "ivyea code run \"<goal>\" --root .",
        ]
    return {
        "root": str(root_path),
        "method": method,
        "target": str(target),
        "execute": False,
        "commands": commands,
        "notes": [
            "这是 dry-run 沙箱计划，不会创建目录或 worktree。",
            "沙箱内试改通过后，再把 patch spec 带回主工作区 validate/apply。",
        ],
    }


def _impact_targets(plan: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in ctx.get("files") or []:
        for definition in item.get("definitions") or []:
            name = str(definition.get("qualname") or definition.get("name") or "")
            if name and name not in out:
                out.append(name)
            if len(out) >= 4:
                return out
    for path in plan.get("relevant_files") or []:
        if path and path not in out:
            out.append(path)
        if len(out) >= 4:
            break
    return out


def _planned_rounds(max_rounds: int, patch_stage: dict[str, Any], test_result: dict[str, Any] | None, repair: dict[str, Any] | None) -> list[dict[str, Any]]:
    count = max(1, max_rounds)
    rounds = [{
        "round": 1,
        "status": "planned",
        "patch_status": patch_stage.get("status"),
        "test_status": "not_run" if test_result is None else ("passed" if test_result.get("ok") else "failed"),
        "repair_status": "ready" if repair else "not_needed" if test_result and test_result.get("ok") else "pending_test",
    }]
    for i in range(2, count + 1):
        rounds.append({
            "round": i,
            "status": "blocked_waiting_for_previous_round",
            "patch_status": "pending_repair_input",
            "test_status": "pending_patch_apply",
            "repair_status": "pending_failure_output",
        })
    return rounds


def review_ready(root: str | Path = ".", staged: bool = False) -> dict[str, Any]:
    status = git_workflow.status(root)
    diff = git_workflow.diff_summary(root, staged=staged)
    review = code_review.review_diff(root, staged=staged)
    tests = patcher.suggested_tests(root)
    return {
        "ok": bool(status.get("ok")) and bool(diff.get("ok")) and bool(review.get("ok")),
        "status": status,
        "diff": diff,
        "review": review,
        "suggested_tests": tests,
        "ready": bool(status.get("ok")) and not any(f.get("severity") == "high" for f in review.get("findings", [])),
    }


def render_plan(plan: dict[str, Any]) -> str:
    lines = ["Code Plan", "", f"- goal: {plan.get('goal')}", f"- root: {plan.get('root')}"]
    if plan.get("relevant_files"):
        lines.extend(["", "Relevant Files"])
        lines.extend(f"- {p}" for p in plan["relevant_files"])
    conventions = plan.get("conventions") or {}
    if conventions.get("files") or conventions.get("suggested_commands"):
        lines.extend(["", "Repo Conventions"])
        for item in conventions.get("files", [])[:4]:
            lines.append(f"- {item.get('path')}")
        for cmd in conventions.get("suggested_commands", [])[:4]:
            lines.append(f"- test: `{cmd}`")
    if plan.get("steps"):
        lines.extend(["", "Steps"])
        lines.extend(f"{i}. {step}" for i, step in enumerate(plan["steps"], start=1))
    if plan.get("suggested_tests"):
        lines.extend(["", "Suggested Tests"])
        lines.extend(f"- `{cmd}`" for cmd in plan["suggested_tests"])
    if plan.get("risks"):
        lines.extend(["", "Risks"])
        lines.extend(f"- {risk}" for risk in plan["risks"])
    lines.extend(["", f"Policy: {plan.get('write_policy')}"])
    return "\n".join(lines)


def render_context(ctx: dict[str, Any]) -> str:
    lines = ["Code Context", "", f"- goal: {ctx.get('goal')}", f"- root: {ctx.get('root')}"]
    for item in ctx.get("files") or []:
        lines.extend(["", f"## {item.get('path')}", f"- language: {item.get('language')}", f"- lines: {item.get('lines')}"])
        defs = item.get("definitions") or []
        if defs:
            lines.append("- definitions: " + ", ".join(f"{d.get('qualname')}:{d.get('lineno')}" for d in defs[:8]))
        if item.get("imports"):
            lines.append("- imports: " + ", ".join(item["imports"][:10]))
        if item.get("calls"):
            lines.append("- calls: " + ", ".join(item["calls"][:12]))
        if item.get("preview"):
            lines.extend(["", "```text", item["preview"][:900], "```"])
    conventions = ctx.get("conventions") or {}
    if conventions.get("files"):
        lines.extend(["", "Repo Conventions"])
        for item in conventions["files"][:3]:
            lines.extend([f"## {item.get('path')}", "```text", item.get("summary", "")[:900], "```"])
    return "\n".join(lines)


def render_brief(data: dict[str, Any]) -> str:
    lines = [
        "Code Brief",
        "",
        f"- goal: {data.get('goal')}",
        f"- root: {data.get('root')}",
        f"- budget: {data.get('budget')}",
        f"- used: {data.get('used')}",
    ]
    for section in data.get("sections") or []:
        lines.extend(["", f"## {section.get('name')}", "```text", section.get("content", ""), "```"])
    return "\n".join(lines)


def render_repair(plan: dict[str, Any]) -> str:
    lines = ["Code Repair Plan", "", f"- root: {plan.get('root')}", f"- failures: {plan.get('failure_count')}"]
    for failure in plan.get("failures") or []:
        lines.append(f"- {failure.get('nodeid')}")
        if failure.get("exception_type"):
            lines.append(f"  exception: {failure.get('exception_type')} {failure.get('exception') or ''}".rstrip())
        if failure.get("reason"):
            lines.append(f"  reason: {failure.get('reason')}")
        if failure.get("source"):
            lines.append(f"  source: {failure.get('source')}")
        if failure.get("frames"):
            first = failure["frames"][0]
            lines.append(f"  frame: {first.get('path')}:{first.get('line')} in {first.get('function')}")
        if failure.get("errors"):
            lines.append("  error: " + " | ".join(failure["errors"][:3]))
    if plan.get("failure_summary"):
        lines.extend(["", "Failure Summary"])
        for item in plan["failure_summary"]:
            lines.append(f"- {item.get('nodeid') or 'unknown'}")
            lines.append(f"  kind: {item.get('kind')}")
            if item.get("exception_type"):
                lines.append(f"  exception: {item.get('exception_type')}")
            if item.get("rerun"):
                lines.append(f"  rerun: `{item.get('rerun')}`")
            if item.get("likely_files"):
                lines.append("  files: " + ", ".join(item["likely_files"][:5]))
            if item.get("action"):
                lines.append(f"  action: {item.get('action')}")
    if plan.get("likely_files"):
        lines.extend(["", "Likely Files"])
        lines.extend(f"- {p}" for p in plan["likely_files"])
    if plan.get("focused_tests"):
        lines.extend(["", "Focused Tests"])
        lines.extend(f"- `{cmd}`" for cmd in plan["focused_tests"])
    if plan.get("repair_loop"):
        lines.extend(["", "Repair Loop"])
        lines.extend(f"{i}. {step}" for i, step in enumerate(plan["repair_loop"], start=1))
    lines.extend(["", "Next Steps"])
    lines.extend(f"{i}. {step}" for i, step in enumerate(plan.get("next_steps") or [], start=1))
    return "\n".join(lines)


def render_test_result(result: dict[str, Any]) -> str:
    base = patcher.render_test_result(result)
    parsed = result.get("parsed") or {}
    if not parsed.get("failure_count"):
        return base
    return base + "\n\n" + render_repair({"root": "", "failure_count": parsed["failure_count"], "failures": parsed["failures"], "likely_files": [], "next_steps": []})


def render_impact(data: dict[str, Any]) -> str:
    text = workspace.render_impact(data)
    steps = data.get("next_steps") or []
    if not steps:
        return text
    return text + "\n\nNext Steps\n" + "\n".join(f"{i}. {step}" for i, step in enumerate(steps, start=1))


def render_patch_candidate(data: dict[str, Any]) -> str:
    lines = [
        "Code Patch Candidate",
        "",
        f"- goal: {data.get('goal')}",
        f"- root: {data.get('root')}",
        f"- status: {data.get('status')}",
    ]
    if data.get("candidate_files"):
        lines.extend(["", "Candidate Files"])
        lines.extend(f"- {path}" for path in data["candidate_files"])
    if data.get("request"):
        lines.extend(["", "LLM Request", "## system", "```text", data["request"].get("system", ""), "```", "## user", "```json", data["request"].get("user", ""), "```"])
    if data.get("raw") and not data.get("spec"):
        lines.extend(["", "Raw Output", "```text", data.get("raw", ""), "```"])
    if data.get("error"):
        lines.extend(["", f"Error: {data.get('error')}"])
    if not data.get("spec"):
        if data.get("instructions"):
            lines.extend(["", "Instructions"])
            lines.extend(f"{i}. {step}" for i, step in enumerate(data["instructions"], start=1))
        return "\n".join(lines)
    lines.extend(["", "Patch Spec", "```json"])
    lines.append(json.dumps(data.get("spec") or {"ops": []}, ensure_ascii=False, indent=2))
    lines.append("```")
    validation = data.get("validation")
    if validation is not None:
        lines.extend(["", "Validation", f"- ok: {validation.get('ok')}"])
        for op in validation.get("ops") or []:
            lines.append(f"- {op.get('path')}: {op.get('message') or 'OK'}")
    if data.get("instructions"):
        lines.extend(["", "Instructions"])
        lines.extend(f"{i}. {step}" for i, step in enumerate(data["instructions"], start=1))
    return "\n".join(lines)


def render_run(data: dict[str, Any]) -> str:
    lines = [
        "Code Run",
        "",
        f"- id: {data.get('id')}",
        f"- goal: {data.get('goal')}",
        f"- root: {data.get('root')}",
        f"- mode: {data.get('mode')}",
        f"- max_rounds: {data.get('max_rounds')}",
    ]
    plan = data.get("plan") or {}
    if plan.get("relevant_files"):
        lines.extend(["", "Relevant Files"])
        lines.extend(f"- {path}" for path in plan["relevant_files"][:12])
    if data.get("impact_targets"):
        lines.extend(["", "Impact Targets"])
        lines.extend(f"- {target}" for target in data["impact_targets"])
    patch = data.get("patch") or {}
    lines.extend(["", "Patch"])
    lines.append(f"- status: {patch.get('status')}")
    if patch.get("candidate_files"):
        lines.append("- candidate_file: " + str(patch["candidate_files"][0]))
    if data.get("selected_tests"):
        lines.extend(["", "Selected Tests"])
        lines.extend(f"- `{cmd}`" for cmd in data["selected_tests"])
    if data.get("rounds"):
        lines.extend(["", "Rounds"])
        for row in data["rounds"]:
            lines.append(
                f"- #{row.get('round')} {row.get('status')} "
                f"patch={row.get('patch_status')} test={row.get('test_status')} repair={row.get('repair_status')}"
            )
    if data.get("test_result"):
        result = data["test_result"]
        lines.extend(["", "Test Result", f"- command: {result.get('command')}", f"- ok: {result.get('ok')}", f"- returncode: {result.get('returncode')}"])
    if data.get("repair"):
        lines.extend(["", "Repair"])
        lines.append(f"- failures: {data['repair'].get('failure_count')}")
    review = data.get("review") or {}
    lines.extend(["", "Review Gate", f"- ok: {review.get('ok')}", f"- ready: {review.get('ready')}", f"- findings: {len(review.get('review', {}).get('findings') or [])}"])
    if data.get("next_steps"):
        lines.extend(["", "Next Steps"])
        lines.extend(f"{i}. {step}" for i, step in enumerate(data["next_steps"], start=1))
    return "\n".join(lines)


def render_bundle(data: dict[str, Any]) -> str:
    lines = [
        "Code Task Bundle",
        "",
        f"- id: {data.get('id')}",
        f"- goal: {data.get('goal')}",
        f"- root: {data.get('root')}",
        f"- mode: {data.get('mode')}",
    ]
    phases = data.get("phases") or []
    if phases:
        lines.extend(["", "Phases"])
        for phase in phases:
            lines.append(f"- {phase.get('name')}: {phase.get('status')} - {phase.get('summary')}")
    plan = data.get("plan") or {}
    if plan.get("relevant_files"):
        lines.extend(["", "Relevant Files"])
        lines.extend(f"- {path}" for path in plan["relevant_files"][:12])
    if data.get("impact_targets"):
        lines.extend(["", "Impact Targets"])
        lines.extend(f"- {target}" for target in data["impact_targets"])
    if data.get("selected_tests"):
        lines.extend(["", "Selected Tests"])
        lines.extend(f"- `{cmd}`" for cmd in data["selected_tests"])
    repair = data.get("repair")
    if repair:
        lines.extend(["", "Repair"])
        for item in repair.get("failure_summary") or []:
            lines.append(f"- {item.get('nodeid')}: {item.get('kind')} `{item.get('rerun')}`")
    review = data.get("review") or {}
    lines.extend(["", "Review Gate"])
    lines.append(f"- ready: {review.get('ready')}")
    lines.append(f"- findings: {len(review.get('review', {}).get('findings') or [])}")
    if data.get("resume_prompt"):
        lines.extend(["", "Resume Prompt", "```text", data["resume_prompt"], "```"])
    if data.get("next_steps"):
        lines.extend(["", "Next Steps"])
        lines.extend(f"{i}. {step}" for i, step in enumerate(data["next_steps"], start=1))
    return "\n".join(lines)


def render_run_list(rows: list[dict[str, Any]]) -> str:
    lines = ["Code Runs", "", f"- count: {len(rows)}"]
    if not rows:
        lines.append("")
        lines.append("暂无 code run 记录。")
        return "\n".join(lines)
    lines.append("")
    for row in rows:
        lines.append(
            f"- {row.get('id')} · {row.get('mode')} · patch={row.get('patch_status')} "
            f"review_ready={row.get('review_ready')} · {row.get('goal')}"
        )
        if row.get("started_at") or row.get("root"):
            lines.append(f"  {row.get('started_at')} · {row.get('root')}")
    return "\n".join(lines)


def render_sandbox_plan(data: dict[str, Any]) -> str:
    lines = [
        "Code Sandbox Plan",
        "",
        f"- root: {data.get('root')}",
        f"- method: {data.get('method')}",
        f"- target: {data.get('target')}",
        f"- execute: {data.get('execute')}",
        "",
        "Commands",
    ]
    lines.extend(f"- `{cmd}`" for cmd in data.get("commands") or [])
    if data.get("notes"):
        lines.extend(["", "Notes"])
        lines.extend(f"- {note}" for note in data["notes"])
    return "\n".join(lines)


def render_quality(data: dict[str, Any]) -> str:
    findings = data.get("findings") or []
    lines = [
        "Code Quality",
        "",
        f"- root: {data.get('root')}",
        f"- files: {data.get('file_count')}",
        f"- tests: {data.get('test_count')}",
        f"- findings: {len(findings)}",
    ]
    if not findings:
        lines.append("")
        lines.append("未发现确定性质量规则命中的问题。")
        return "\n".join(lines)
    lines.append("")
    for item in findings[:30]:
        loc = item.get("path") or "global"
        if item.get("line"):
            loc += f":{item.get('line')}"
        lines.append(f"- [{item.get('severity')}] {loc} · {item.get('title')}")
        if item.get("detail"):
            lines.append(f"  {item.get('detail')}")
    return "\n".join(lines)


def render_diff_brief(data: dict[str, Any]) -> str:
    diff = data.get("diff") or {}
    review = data.get("review") or {}
    lines = [
        "Code Diff Brief",
        "",
        f"- root: {data.get('root')}",
        f"- staged: {data.get('staged')}",
        f"- changed_files: {len(diff.get('files') or [])}",
        f"- findings: {len(review.get('findings') or [])}",
    ]
    if data.get("bullets"):
        lines.extend(["", "Summary"])
        lines.extend(f"- {item}" for item in data["bullets"])
    if data.get("suggested_tests"):
        lines.extend(["", "Suggested Tests"])
        lines.extend(f"- `{cmd}`" for cmd in data["suggested_tests"])
    lines.extend(["", "PR Draft", "```markdown", data.get("pr_body", ""), "```"])
    return "\n".join(lines)


def render_release_check(data: dict[str, Any]) -> str:
    lines = [
        "Code Release Check",
        "",
        f"- root: {data.get('root')}",
        f"- version: {data.get('version') or '(unknown)'}",
        f"- ok: {data.get('ok')}",
    ]
    if data.get("hard_blockers"):
        lines.extend(["", "Hard Blockers"])
        lines.extend(f"- {item}" for item in data["hard_blockers"])
    if data.get("warnings"):
        lines.extend(["", "Warnings"])
        lines.extend(f"- {item}" for item in data["warnings"])
    if data.get("suggested_tests"):
        lines.extend(["", "Suggested Tests"])
        lines.extend(f"- `{cmd}`" for cmd in data["suggested_tests"])
    release_plan = data.get("release_plan") or {}
    lines.extend(["", "Release Plan", f"- ok: {release_plan.get('ok')}"])
    if release_plan.get("error"):
        lines.append(f"- error: {release_plan.get('error')}")
    review = data.get("review") or {}
    lines.extend(["", "Review Gate", f"- ready: {review.get('ready')}", f"- findings: {len(review.get('review', {}).get('findings') or [])}"])
    quality_data = data.get("quality") or {}
    lines.extend(["", "Quality", f"- findings: {len(quality_data.get('findings') or [])}"])
    return "\n".join(lines)


def render_refs(data: dict[str, Any]) -> str:
    matches = data.get("matches") or []
    lines = ["Code Refs", "", f"- root: {data.get('root')}", f"- symbol: {data.get('symbol')}", f"- matches: {len(matches)}"]
    for item in matches[:80]:
        loc = item.get("path") or ""
        if item.get("line"):
            loc += f":{item.get('line')}"
        lines.append(f"- {item.get('kind')} {loc} · {item.get('text')}")
    return "\n".join(lines)


def render_rename_plan(data: dict[str, Any]) -> str:
    lines = [
        "Code Rename Plan",
        "",
        f"- root: {data.get('root')}",
        f"- symbol: {data.get('symbol')}",
        f"- new_name: {data.get('new_name')}",
        f"- matches: {len(data.get('matches') or [])}",
        f"- ops: {len((data.get('spec') or {}).get('ops') or [])}",
    ]
    if data.get("warnings"):
        lines.extend(["", "Warnings"])
        lines.extend(f"- {warning}" for warning in data["warnings"])
    if data.get("suggested_tests"):
        lines.extend(["", "Suggested Tests"])
        lines.extend(f"- `{cmd}`" for cmd in data["suggested_tests"])
    validation = data.get("validation")
    if validation is not None:
        lines.extend(["", "Validation", f"- ok: {validation.get('ok')}"])
    lines.extend(["", "Patch Spec", "```json"])
    lines.append(json.dumps(data.get("spec") or {"ops": []}, ensure_ascii=False, indent=2))
    lines.append("```")
    if data.get("instructions"):
        lines.extend(["", "Instructions"])
        lines.extend(f"{i}. {step}" for i, step in enumerate(data["instructions"], start=1))
    return "\n".join(lines)


def render_review(result: dict[str, Any]) -> str:
    lines = [
        "Code Review Gate",
        "",
        f"- ok: {result.get('ok')}",
        f"- ready: {result.get('ready')}",
        f"- clean: {result.get('status', {}).get('clean')}",
        f"- changed_files: {len(result.get('diff', {}).get('files') or [])}",
        f"- findings: {len(result.get('review', {}).get('findings') or [])}",
    ]
    if result.get("suggested_tests"):
        lines.extend(["", "Suggested Tests"])
        lines.extend(f"- `{cmd}`" for cmd in result["suggested_tests"])
    findings = result.get("review", {}).get("findings") or []
    if findings:
        lines.extend(["", "Findings"])
        for finding in findings[:12]:
            loc = f"{finding.get('path')}:{finding.get('line')}" if finding.get("path") and finding.get("line") else finding.get("path") or "global"
            lines.append(f"- [{finding.get('severity')}] {loc} · {finding.get('title')}")
    return "\n".join(lines)
