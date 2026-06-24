"""通用工具层 —— 让 agent 不止会广告巡检，能干真实运营活。

读类（read_file/list_dir/web_fetch/web_search）自动放行；写/执行类
（write_file/edit_file/run_python/run_command）经人工审批门控（复用 permission），
且在计划模式下一律拒绝。执行类带沙箱：限工作目录、超时、输出截断。

设计取自 Claude API agent-design：读广、写/执行经门控（可审计、可拦截）。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import config, panels, permission, policy, security

_MAX_OUT = 4000          # 工具返回截断（防爆上下文）
_EXEC_TIMEOUT = 30       # 执行类默认超时（秒）
_MUTATING = {"write_file", "edit_file", "run_python", "run_command"}
_DANGEROUS_COMMANDS = [
    r"\brm\s+-rf\s+/(?:\s|$)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bmkfs(?:\.[\w-]+)?\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bdd\s+if=.*\s+of=/dev/",
]


def _truncate(s: str, n: int = _MAX_OUT) -> str:
    s = security.redact_text(s)
    return s if len(s) <= n else s[:n] + f"\n…（已截断，共 {len(s)} 字）"


def _dangerous_command(command: str) -> str:
    for pattern in _DANGEROUS_COMMANDS:
        if re.search(pattern, command, re.I):
            return pattern
    return ""


def _gate(ctx, kind: str, preview: str) -> tuple[bool, str]:
    """写/执行前门控：计划模式拒绝；否则人工审批。返回 (放行?, 拒绝消息)。"""
    if getattr(ctx, "plan_mode", False):
        return False, f"计划模式（只读）：不执行 {kind}。请先给计划，/approve 后再做。"
    decision = permission.request_intent({"op_type": kind}, preview, ctx.perm)
    if decision == permission.APPROVE:
        return True, ""
    if decision == permission.ABORT:
        return False, "用户终止。"
    return False, f"已跳过：{preview}"


# ── 读类（自动放行）──────────────────────────────────────────────────────────
def t_read_file(args: dict, ctx) -> str:
    p = Path(os.path.expanduser(args.get("path", ""))).resolve()
    ok, msg = policy.check_path(p, "read")
    if not ok:
        return msg
    if not p.exists():
        return f"文件不存在：{p}"
    if p.is_dir():
        return f"{p} 是目录，请用 list_dir。"
    try:
        return _truncate(p.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        return f"读取失败：{e}"


def t_list_dir(args: dict, ctx) -> str:
    p = Path(os.path.expanduser(args.get("path", ".") or ".")).resolve()
    ok, msg = policy.check_path(p, "read")
    if not ok:
        return msg
    if not p.exists():
        return f"目录不存在：{p}"
    if p.is_file():
        return f"{p.name}\t{p.stat().st_size} bytes"
    try:
        rows = []
        for c in sorted(p.iterdir()):
            kind = "d" if c.is_dir() else "f"
            size = c.stat().st_size if c.is_file() else ""
            rows.append(f"  [{kind}] {c.name}\t{size}")
        return f"{p}（{len(rows)} 项）：\n" + _truncate("\n".join(rows))
    except Exception as e:  # noqa: BLE001
        return f"列目录失败：{e}"


def t_web_fetch(args: dict, ctx) -> str:
    url = args.get("url", "")
    if not url.startswith(("http://", "https://")):
        return "url 必须以 http(s):// 开头。"
    try:
        import httpx
        r = httpx.get(url, timeout=30, follow_redirects=True,
                      headers={"User-Agent": "ivyea-agent/0.2"})
    except Exception as e:  # noqa: BLE001
        return f"抓取失败：{e}"
    if r.status_code >= 400:
        return f"HTTP {r.status_code}"
    text = r.text
    ct = r.headers.get("content-type", "")
    if "html" in ct:
        import re
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
    return _truncate(text)


def t_web_search(args: dict, ctx) -> str:
    """尽力而为：DuckDuckGo lite（无 key，可能受限）。"""
    q = args.get("query", "")
    if not q:
        return "query 为空。"
    try:
        import re

        import httpx
        r = httpx.post("https://lite.duckduckgo.com/lite/", data={"q": q}, timeout=30,
                       headers={"User-Agent": "Mozilla/5.0"})
        rows = re.findall(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
        if not rows:
            rows = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
        out = []
        for href, title in rows[:8]:
            title = re.sub(r"<[^>]+>", "", title).strip()
            if title:
                out.append(f"  · {title}\n    {href}")
        return "\n".join(out) if out else "（无结果，或搜索源受限）"
    except Exception as e:  # noqa: BLE001
        return f"搜索失败（尽力而为）：{e}"


# ── 代码导航（只读，自动放行）────────────────────────────────────────────────
_GREP_BINARY = re.compile(rb"\x00")


def _ws_root(ctx):
    from . import workspace
    return workspace.resolve_root(getattr(ctx, "workspace", "") or None)


def t_grep(args: dict, ctx) -> str:
    """内容正则搜索（ripgrep 风格），返回 file:line 命中行。只读，自动放行。"""
    pattern = args.get("pattern", "")
    if not pattern:
        return "pattern 为空。"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"正则无效：{e}"
    from . import workspace
    root = _ws_root(ctx)
    ok, msg = policy.check_path(root, "read")
    if not ok:
        return msg
    glob = (args.get("glob") or "").strip()
    max_hits = min(int(args.get("max_results") or 80), 300)
    hits: list[str] = []
    scanned = 0
    for path in workspace.iter_files(root):
        if glob and not path.match(glob):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if _GREP_BINARY.search(raw[:4096]):
            continue  # 跳过二进制
        scanned += 1
        text = raw.decode("utf-8", errors="replace")
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= max_hits:
                    break
        if len(hits) >= max_hits:
            break
    if not hits:
        return f"无匹配（扫描 {scanned} 文件）：{pattern}"
    return f"命中 {len(hits)} 处（扫描 {scanned} 文件）：\n" + _truncate("\n".join(hits))


def t_code_search(args: dict, ctx) -> str:
    """按符号/路径/预览检索代码库相关文件（索引搜索）。只读。"""
    from . import workspace
    q = args.get("query", "")
    if not q:
        return "query 为空。"
    rows = workspace.search(q, root=_ws_root(ctx), limit=min(int(args.get("limit") or 10), 30))
    return _truncate(workspace.render_search(rows, q))


def t_code_symbols(args: dict, ctx) -> str:
    """列出/搜索代码库里的函数/类等符号定义位置。只读。"""
    from . import workspace
    data = workspace.symbol_index(root=_ws_root(ctx), query=args.get("query", "") or "",
                                  limit=min(int(args.get("limit") or 40), 120))
    return _truncate(workspace.render_symbols(data))


def t_code_impact(args: dict, ctx) -> str:
    """查某个符号/文件的调用方、导入方与受影响测试。只读。"""
    from . import workspace
    target = args.get("target", "")
    if not target:
        return "target 为空。"
    data = workspace.impact_analysis(target, root=_ws_root(ctx), limit=min(int(args.get("limit") or 60), 120))
    return _truncate(workspace.render_impact(data))


# ── 代码闭环（结构化补丁 / 测试 / 修复计划）──────────────────────────────────
def t_code_apply_patch(args: dict, ctx) -> str:
    """校验/应用结构化补丁并跑测试。默认 dry-run（只校验、不写）；execute=true 才真写，
    且经人工审批。补丁 ops=[{path, old, new}]，old 必须在文件中唯一出现。"""
    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        return "ops 为空：需要 [{path, old, new}, ...]，old 必须在文件中唯一出现。"
    execute = bool(args.get("execute"))
    if execute:
        preview = "应用结构化补丁并执行测试：\n" + "\n".join(
            f"  · {o.get('path')}" for o in ops if isinstance(o, dict))
        ok, msg = _gate(ctx, "code_apply_patch", preview)
        if not ok:
            return msg
    from . import code_agent
    try:
        result = code_agent.patch_apply_loop(
            {"ops": ops}, root=_ws_root(ctx),
            test_command=args.get("test_command", "") or "", execute=execute)
    except Exception as e:  # noqa: BLE001
        return f"补丁执行出错：{e}"
    return _truncate(code_agent.render_run(result))


def t_run_tests(args: dict, ctx) -> str:
    """在工作目录跑测试命令并返回结果（执行，会弹审批）。默认 `python -m pytest`。"""
    command = (args.get("command") or "python -m pytest").strip()
    ok, msg = _gate(ctx, "run_tests", "运行测试：" + command)
    if not ok:
        return msg
    from . import code_agent
    res = code_agent.run_tests(command, root=_ws_root(ctx), timeout=int(args.get("timeout") or 120))
    head = "✓ 测试通过" if res.get("ok") else f"✗ 测试失败（exit {res.get('returncode')}）"
    return _truncate(head + "\n" + (res.get("output") or ""))


def t_code_repair(args: dict, ctx) -> str:
    """解析失败的测试输出，生成下一轮修复计划（可疑文件/失败摘要/重跑命令）。只读。"""
    output = args.get("test_output", "") or ""
    if not output.strip():
        return "test_output 为空：把失败的测试输出贴进来。"
    from . import code_agent
    return _truncate(code_agent.render_repair(code_agent.repair_plan(output, root=_ws_root(ctx))))


# ── 写/执行类（门控）─────────────────────────────────────────────────────────
def t_write_file(args: dict, ctx) -> str:
    path = os.path.expanduser(args.get("path", ""))
    content = args.get("content", "")
    if not path:
        return "path 为空。"
    p = Path(path).resolve()
    ok, msg = policy.check_path(p, "write")
    if not ok:
        return msg
    exists = p.exists()
    preview = f"写文件 {p}（{'覆盖' if exists else '新建'}，{len(content)} 字）"
    if exists:
        try:
            preview += "\n" + panels.render_diff(p.read_text(encoding="utf-8"), content, p.name)
        except Exception:
            pass
    ok, msg = _gate(ctx, "write_file", preview)
    if not ok:
        return msg
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {p}（{len(content)} 字）"
    except Exception as e:  # noqa: BLE001
        return f"写入失败：{e}"


def t_edit_file(args: dict, ctx) -> str:
    p = Path(os.path.expanduser(args.get("path", ""))).resolve()
    ok, msg = policy.check_path(p, "write")
    if not ok:
        return msg
    old, new = args.get("old", ""), args.get("new", "")
    if not p.exists():
        return f"文件不存在：{p}"
    if not old:
        return "old 为空（要替换的原文）。"
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return f"读取失败：{e}"
    cnt = text.count(old)
    if cnt == 0:
        return "未找到要替换的原文（old 不匹配）。"
    if cnt > 1:
        return f"原文出现 {cnt} 次，不唯一；请提供更长的 old 以唯一定位。"
    preview = f"编辑 {p}：替换 1 处\n" + panels.render_diff(old, new, p.name)
    ok, msg = _gate(ctx, "edit_file", preview)
    if not ok:
        return msg
    try:
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"已编辑 {p}（替换 1 处）"
    except Exception as e:  # noqa: BLE001
        return f"编辑失败：{e}"


def _make_preexec(timeout: int):
    """POSIX 资源限额（在子进程 fork 后、exec 前生效）：内存(地址空间)、CPU 时间、
    单文件大小、禁 core dump。Windows / 无 resource 模块时返回 None（优雅降级）。"""
    if os.name == "nt":
        return None
    try:
        import resource
    except ImportError:
        return None
    mem_mb = int(config.get_setting("exec_memory_limit_mb", 2048))
    fsize_mb = int(config.get_setting("exec_file_limit_mb", 512))
    cpu_s = max(1, int(timeout)) + 5

    def _apply():
        limits = [(resource.RLIMIT_CPU, cpu_s, cpu_s + 5), (resource.RLIMIT_CORE, 0, 0)]
        if mem_mb > 0:
            b = mem_mb * 1024 * 1024
            limits.append((resource.RLIMIT_AS, b, b))  # 内存上限：Linux 强制；macOS 忽略 RLIMIT_AS
        if fsize_mb > 0:
            b = fsize_mb * 1024 * 1024
            limits.append((resource.RLIMIT_FSIZE, b, b))
        for res, soft, hard in limits:
            try:
                resource.setrlimit(res, (soft, hard))
            except (ValueError, OSError):
                pass

    return _apply


def _run(cmd, args, ctx, kind: str, preview: str) -> str:
    ok, msg = _gate(ctx, kind, preview)
    if not ok:
        return msg
    workdir = getattr(ctx, "workspace", "") or os.getcwd()
    timeout = int(args.get("timeout") or _EXEC_TIMEOUT)
    try:
        proc = subprocess.run(cmd, cwd=workdir, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace",
                              preexec_fn=_make_preexec(timeout))
    except subprocess.TimeoutExpired:
        return f"超时（>{timeout}s）已终止。"
    except Exception as e:  # noqa: BLE001
        return f"执行失败：{e}"
    out = proc.stdout or ""
    head = f"[退出码 {proc.returncode}]\n"
    return head + (_truncate(out) if out else "（无输出）")


def t_run_python(args: dict, ctx) -> str:
    import sys
    code = args.get("code", "")
    if not code:
        return "code 为空。"
    return _run([sys.executable, "-c", code], args, ctx, "run_python",
                "运行 Python：\n" + _truncate(code, 600))


def t_run_command(args: dict, ctx) -> str:
    command = args.get("command", "")
    if not command:
        return "command 为空。"
    ok, msg = policy.check_command(command)
    if not ok:
        return msg
    blocked = _dangerous_command(command)
    if blocked:
        return f"安全策略拒绝高风险命令：{blocked}"
    import os as _os
    shell = ["cmd", "/c", command] if _os.name == "nt" else ["bash", "-lc", command]
    return _run(shell, args, ctx, "run_command", "运行命令：" + _truncate(command, 400))


def t_todo_write(args: dict, ctx) -> str:
    """更新任务计划（供长任务可视化）。todos:[{content,status}]。"""
    todos = args.get("todos") or []
    clean = []
    for t in todos:
        if isinstance(t, dict) and t.get("content"):
            st = t.get("status", "pending")
            clean.append({"content": str(t["content"]),
                          "status": st if st in ("pending", "in_progress", "completed") else "pending"})
    ctx.todos = clean
    done = sum(1 for t in clean if t["status"] == "completed")
    return f"已更新计划：{done}/{len(clean)} 完成。" if clean else "计划已清空。"


def _task_id(args: dict, ctx) -> str:
    return str(args.get("task_id") or getattr(ctx, "task_id", "") or "").strip()


def t_task_read(args: dict, ctx) -> str:
    task_id = _task_id(args, ctx)
    if not task_id:
        return "未绑定 task_id。请用 ivyea chat --task-id <id>，或在工具参数传 task_id。"
    try:
        from . import task_runner
        return _truncate(task_runner.render(task_runner.load(task_id)))
    except Exception as e:  # noqa: BLE001
        return f"读取任务失败：{e}"


def t_task_step(args: dict, ctx) -> str:
    task_id = _task_id(args, ctx)
    if not task_id:
        return "未绑定 task_id，无法更新任务步骤。"
    try:
        from . import task_runner
        task = task_runner.update_step(
            task_id,
            int(args.get("index") or 1),
            str(args.get("status") or ""),
            note=str(args.get("notes") or args.get("note") or ""),
        )
        return _truncate(task_runner.render(task))
    except Exception as e:  # noqa: BLE001
        return f"更新任务步骤失败：{e}"


def t_task_log(args: dict, ctx) -> str:
    task_id = _task_id(args, ctx)
    if not task_id:
        return "未绑定 task_id，无法写入任务日志。"
    try:
        from . import task_runner
        task = task_runner.append_log(
            task_id,
            str(args.get("text") or args.get("notes") or ""),
            kind=str(args.get("kind") or "agent"),
        )
        return _truncate(task_runner.render(task))
    except Exception as e:  # noqa: BLE001
        return f"写入任务日志失败：{e}"


def t_task_resume(args: dict, ctx) -> str:
    task_id = _task_id(args, ctx)
    if not task_id:
        return "未绑定 task_id，无法读取续跑提示。"
    try:
        from . import task_runner
        data = task_runner.resume_payload(task_id)
        resume = data.get("resume") or {}
        return _truncate(str(resume.get("prompt") or task_runner.render_resume(data["task"])))
    except Exception as e:  # noqa: BLE001
        return f"读取续跑提示失败：{e}"


# ── schema + dispatch ────────────────────────────────────────────────────────
def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


GENERAL_TOOL_SCHEMAS = [
    _fn("read_file", "读取本地文本文件内容（只读，自动放行）。",
        {"path": {"type": "string", "description": "文件路径，支持 ~"}}, ["path"]),
    _fn("list_dir", "列出目录内容（只读）。",
        {"path": {"type": "string", "description": "目录路径，默认当前目录"}}),
    _fn("write_file", "写入/覆盖本地文件（写操作，会弹人工审批）。",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("edit_file", "对文件做唯一字符串替换（写操作，会弹审批）。old 必须在文件中唯一出现。",
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        ["path", "old", "new"]),
    _fn("run_python", "在沙箱(限工作目录/超时)运行 Python 代码取 stdout（执行，会弹审批）。可用 pandas/openpyxl 等。",
        {"code": {"type": "string"}, "timeout": {"type": "integer", "description": "秒，默认30"}}, ["code"]),
    _fn("run_command", "在沙箱运行 shell 命令取输出（执行，会弹审批）。",
        {"command": {"type": "string"}, "timeout": {"type": "integer"}}, ["command"]),
    _fn("web_fetch", "抓取一个 URL 的文本内容（GET，只读，自动放行）。",
        {"url": {"type": "string"}}, ["url"]),
    _fn("web_search", "网页搜索关键词（尽力而为，无 key）。",
        {"query": {"type": "string"}}, ["query"]),
    _fn("grep", "在代码库里做内容正则搜索（ripgrep 风格），返回 file:line 命中行。只读，自动放行。找代码先用它，别瞎猜路径。",
        {"pattern": {"type": "string", "description": "正则表达式"},
         "glob": {"type": "string", "description": "可选：只搜匹配此 glob 的文件，如 *.py"},
         "max_results": {"type": "integer", "description": "最多命中条数，默认 80"}}, ["pattern"]),
    _fn("code_search", "按符号/路径/预览检索代码库里最相关的文件（索引搜索）。只读。适合“这功能在哪实现的”。",
        {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    _fn("code_symbols", "列出/搜索代码库里的函数/类等符号定义位置。只读。",
        {"query": {"type": "string", "description": "可选，过滤符号名"}, "limit": {"type": "integer"}}),
    _fn("code_impact", "查某个符号或文件的调用方、导入方与受影响测试（改动影响面）。只读。改代码前先看它。",
        {"target": {"type": "string", "description": "符号名或文件路径"}, "limit": {"type": "integer"}}, ["target"]),
    _fn("code_apply_patch", "校验/应用结构化补丁并跑测试。默认 dry-run 只校验不写；execute=true 才真写(经人工审批)。"
        "ops=[{path,old,new}]，old 必须在文件中唯一出现。多文件/要顺带跑测试时优先用它而非 edit_file。",
        {"ops": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}}},
         "test_command": {"type": "string", "description": "可选：execute 后跑的测试命令"},
         "execute": {"type": "boolean", "description": "true=真写并测试(审批)，默认 false 只校验"}}, ["ops"]),
    _fn("run_tests", "在工作目录跑测试命令并返回结果（执行，会弹审批）。默认 python -m pytest。",
        {"command": {"type": "string"}, "timeout": {"type": "integer"}}),
    _fn("code_repair", "解析失败测试输出，生成下一轮修复计划（可疑文件/失败摘要/重跑命令）。只读。",
        {"test_output": {"type": "string"}}, ["test_output"]),
    _fn("todo_write", "维护多步任务计划(让长任务可视化)。每步 {content, status: pending|in_progress|completed}。开始多步任务时先列计划，完成一步就更新状态。",
        {"todos": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}}}},
        ["todos"]),
    _fn("task_read", "读取当前绑定的 Ivyea 长任务状态、步骤和最近事件。续跑任务时应先调用。",
        {"task_id": {"type": "string", "description": "可选；不传则使用当前对话绑定的 task_id"}}),
    _fn("task_step", "更新当前绑定的 Ivyea 长任务步骤状态。用于执行过程中标记 in_progress/completed/blocked。",
        {
            "task_id": {"type": "string", "description": "可选；不传则使用当前对话绑定的 task_id"},
            "index": {"type": "integer", "description": "步骤序号，从 1 开始"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "completed", "skipped"]},
            "notes": {"type": "string", "description": "步骤备注"},
        },
        ["index", "status"]),
    _fn("task_log", "向当前绑定的 Ivyea 长任务追加执行日志或结论。",
        {
            "task_id": {"type": "string", "description": "可选；不传则使用当前对话绑定的 task_id"},
            "text": {"type": "string"},
            "kind": {"type": "string", "description": "日志类型，默认 agent"},
        },
        ["text"]),
    _fn("task_resume", "读取当前绑定的 Ivyea 长任务结构化续跑提示。",
        {"task_id": {"type": "string", "description": "可选；不传则使用当前对话绑定的 task_id"}}),
]

GENERAL_DISPATCH = {
    "read_file": t_read_file, "list_dir": t_list_dir,
    "write_file": t_write_file, "edit_file": t_edit_file,
    "run_python": t_run_python, "run_command": t_run_command,
    "web_fetch": t_web_fetch, "web_search": t_web_search,
    "grep": t_grep, "code_search": t_code_search,
    "code_symbols": t_code_symbols, "code_impact": t_code_impact,
    "code_apply_patch": t_code_apply_patch, "run_tests": t_run_tests, "code_repair": t_code_repair,
    "todo_write": t_todo_write,
    "task_read": t_task_read,
    "task_step": t_task_step,
    "task_log": t_task_log,
    "task_resume": t_task_resume,
}
