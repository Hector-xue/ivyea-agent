"""通用工具层 —— 让 agent 不止会广告巡检，能干真实运营活。

读类（read_file/list_dir/web_fetch/web_search）自动放行；写/执行类
（write_file/edit_file/run_python/run_command）经人工审批门控（复用 permission），
且在计划模式下一律拒绝。执行类带沙箱：限工作目录、超时、输出截断。

设计取自 Claude API agent-design：读广、写/执行经门控（可审计、可拦截）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import config, panels, permission, policy, security

_MAX_OUT = 4000          # 工具返回截断（防爆上下文）
_EXEC_TIMEOUT = 30       # 执行类默认超时（秒）
DEADEND_MARK = "⚠"       # 死胡同信号前缀（0 文件/无文件/路径错）：供 ui.tool_result 高亮 + 提示模型换策略
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


def _with_line_numbers(text: str, start: int = 1) -> str:
    """cat -n 式给每行加行号（仅供模型定位/引用；edit 的 old 不要带行号）。"""
    lines = text.split("\n")
    width = max(4, len(str(start + len(lines) - 1)))
    return "\n".join(f"{start + i:>{width}}\t{ln}" for i, ln in enumerate(lines))


_LINE_NO_RE = re.compile(r"^\s*\d+\t", re.M)


def _strip_line_no(text: str) -> str:
    """去掉每行前导的 `行号\\t`（兜底模型误把 read_file 的行号粘进 old）。"""
    return _LINE_NO_RE.sub("", text)


def _disp(p) -> str:
    """审批预览用的友好路径：在 cwd 下显示相对路径，否则原样。仅展示用，写入仍用绝对路径。"""
    try:
        rel = os.path.relpath(p, os.getcwd())
        return rel if not rel.startswith("..") else str(p)
    except Exception:
        return str(p)


def _require_read(ctx, paths) -> str:
    """改前必读硬护栏（对标 Claude Code）：本会话未 read_file 过的已存在目标文件，
    直接挡回让模型先读。返回空串=放行；非空=应直接 return 的错误信息。"""
    read = getattr(ctx, "read_paths", None) or set()
    unread = [p for p in paths if str(p) not in read and Path(p).exists()]
    if not unread:
        return ""
    names = "、".join(_disp(p) for p in unread)
    return (f"已拦截：本会话还没 read_file 过 {names}，不能盲改。"
            f"请先 read_file 看真实内容、确认 old 唯一匹配，再重试本次编辑。")


def _gate(ctx, kind: str, preview: str, detail: dict | None = None) -> tuple[bool, str]:
    """写/执行前门控：计划模式拒绝；否则人工审批。返回 (放行?, 拒绝消息)。
    detail：给 policy 档无人值守判定用的结构化信息（command/path），并入 intent。"""
    if getattr(ctx, "plan_mode", False):
        return False, f"计划模式（只读）：不执行 {kind}。请先给计划，/approve 后再做。"
    decision = permission.request_intent({"op_type": kind, **(detail or {})}, preview, ctx.perm)
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
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"读取失败：{e}"
    try:
        ctx.read_paths.add(str(p))   # 记录已读，供 edit_file/code_apply_patch 的改前必读软护栏
    except AttributeError:
        pass
    offset = args.get("offset")
    limit = args.get("limit")
    if offset is None and limit is None:
        return _truncate(_with_line_numbers(text, 1))
    # 行区间读取：offset 从 1 开始；大文件只取一段，避免被迫用 run_command 分段读。
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(offset or 1))
    if start > total:
        return f"（{p.name} 共 {total} 行，offset={start} 超出范围）"
    end = total if limit is None else min(total, start + max(1, int(limit)) - 1)
    body = _with_line_numbers("\n".join(lines[start - 1:end]), start)
    return f"（{p.name} 第 {start}–{end} 行，共 {total} 行）\n" + _truncate(body)


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


def _expand_braces(pattern: str) -> list[str]:
    """把单层花括号 glob 展开成多个模式：**/*.{ts,tsx,js} → [**/*.ts, **/*.tsx, **/*.js]。
    fnmatch/PurePath.match 都不认花括号，缺这一步会静默扫 0 文件。不支持嵌套（够用即可）。"""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    head, tail = pattern[:m.start()], pattern[m.end():]
    out: list[str] = []
    for opt in m.group(1).split(","):
        out.extend(_expand_braces(head + opt + tail))   # 递归展开可能存在的第二组花括号
    return out


def _normalize_glob_pattern(pattern: str, root: Path) -> str:
    """Make common model-produced absolute/repo-prefixed globs root-relative.

    Once task scope has locked ``root=/x/ivyea-agent``, models still sometimes
    emit ``/x/ivyea-agent/**/*.py`` or ``ivyea-agent/**/*.py``. Path matching
    expects a root-relative pattern; normalize only prefixes that provably
    identify the active root and leave outside paths untouched for policy/error
    handling.
    """
    raw = (pattern or "").strip().replace("\\", "/")
    if raw.startswith("./"):
        raw = raw[2:]
    root_text = root.resolve().as_posix().rstrip("/")
    if raw == root_text:
        return "*"
    if raw.startswith(root_text + "/"):
        return raw[len(root_text) + 1:]
    prefix = root.name + "/"
    if root.name and raw.startswith(prefix):
        return raw[len(prefix):]
    return raw


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
    normalized_glob = _normalize_glob_pattern(glob, root) if glob else ""
    globs = _expand_braces(normalized_glob) if normalized_glob else []  # 花括号 + 根相对容错
    max_hits = min(int(args.get("max_results") or 80), 300)
    hits: list[str] = []
    scanned = 0
    for path in workspace.iter_files(root):
        if globs and not any(path.match(g) for g in globs):
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
            rel = path.relative_to(root).as_posix()   # 统一正斜杠（与 t_glob 一致，跨平台稳定；修 Windows 反斜杠）
        except ValueError:
            rel = path.as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= max_hits:
                    break
        if len(hits) >= max_hits:
            break
    if not hits:
        if scanned == 0:   # 死胡同：根/glob 写错，不是"真没匹配"——报红旗、逼换策略而非换关键词重搜
            return (f"{DEADEND_MARK} 扫描了 0 个文件（根 {root}"
                    f"{('，glob=' + glob) if glob else ''}）——多半是搜索根或 glob 写错。"
                    "先用 list_dir 核对根目录，别换关键词重搜。")
        return f"无匹配（扫描 {scanned} 文件）：{pattern}"
    return f"命中 {len(hits)} 处（扫描 {scanned} 文件）：\n" + _truncate("\n".join(hits))


def t_glob(args: dict, ctx) -> str:
    """按文件名 glob 模式找文件（如 **/*.py）。只读、ignore-aware（复用 workspace.iter_files）。"""
    import fnmatch
    from . import workspace
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return "pattern 为空。"
    base = args.get("path")
    root = Path(os.path.expanduser(base)).resolve() if base else Path(_ws_root(ctx))
    ok, msg = policy.check_path(root, "read")
    if not ok:
        return msg
    max_n = min(int(args.get("max_results") or 100), 500)
    # fnmatch 的 * 本就跨 /，把 **/ 归一为空、** 归一为 * 即可正确支持 **/*.py 这类（含根目录）；
    # 并展开 {a,b} 花括号（fnmatch 不认），避免 **/*.{ts,tsx} 静默 0 匹配。
    pats = _expand_braces(_normalize_glob_pattern(pattern, root))
    norms = [p.replace("**/", "").replace("**", "*") for p in pats]
    hits: list[str] = []
    scanned = 0
    for path in workspace.iter_files(root):
        scanned += 1
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        if any(fnmatch.fnmatch(rel, n) for n in norms) or any(fnmatch.fnmatch(path.name, p) for p in pats):
            hits.append(rel)
            if len(hits) >= max_n:
                break
    if not hits:
        if scanned == 0:   # 死胡同：根目录下压根没文件 → path 参数多半写错
            return f"{DEADEND_MARK} 根目录 {root} 下没有文件——检查 path 参数是否写错。"
        return (f"{DEADEND_MARK} 没有匹配 {pattern} 的文件（扫描 {scanned} 文件）"
                "——确认 glob 与路径是否正确，别换关键词反复重搜。")
    return f"匹配 {len(hits)} 个文件：\n" + _truncate("\n".join(sorted(hits)))


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
    """一次性应用结构化补丁并跑测试（多文件/多处关联改动优先用它）。
    内部固定流程：先校验(不写)→失败直接返回；通过则给彩色 diff 预览→一次人工审批→落盘+跑测试。
    补丁 ops=[{path, old, new}]，old 必须在文件中唯一出现。"""
    from . import code_agent, patcher
    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        return "ops 为空：需要 [{path, old, new}, ...]，old 必须在文件中唯一出现。"
    spec = {"ops": ops}
    root = _ws_root(ctx)
    # 1) 先校验，不写。校验失败不弹审批，直接把问题回给模型。
    validation = patcher.validate_spec(spec, root=root)
    if not validation.get("ok"):
        return _truncate(patcher.render_validation(validation))
    # 2) 改前必读硬护栏 + 构造彩色 diff 预览
    abs_paths = [str((Path(root) / o.get("path", "")).resolve()) for o in ops if isinstance(o, dict) and o.get("path")]
    blocked = _require_read(ctx, abs_paths)
    if blocked:
        return blocked
    n = len([o for o in ops if isinstance(o, dict)])
    lines = [f"应用结构化补丁（{n} 处）并跑测试："]
    for o in ops:
        if not isinstance(o, dict):
            continue
        lines.append(f"  · {_disp((Path(root) / o.get('path', '')).resolve())}")
        try:
            lines.append(panels.render_diff(o.get("old", ""), o.get("new", ""), str(o.get("path", ""))))
        except Exception:
            pass
    preview = "\n".join(lines)
    # 3) 一次审批
    _paths = [str((Path(root) / o.get("path", "")).resolve()) for o in ops if isinstance(o, dict)]
    ok, msg = _gate(ctx, "code_apply_patch", preview, detail={"paths": _paths})
    if not ok:
        return msg
    # 4) 落盘 + 跑测试
    try:
        result = code_agent.patch_apply_loop(
            spec, root=root,
            test_command=args.get("test_command", "") or "", execute=True)
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


# ── MCP（连任意已配置的 MCP 服务器：工具/资源/prompt）─────────────────────────
def _mcp_servers() -> dict:
    return config.load_mcp().get("mcpServers", {})


def _mcp_run(server: str, fn):
    """连 server → initialize → fn(client) → close。返回 (result, err_str)；任一为 None。"""
    from .mcp_client import MCPClient, MCPError
    spec = _mcp_servers().get(server)
    if not spec:
        return None, f"未配置 MCP 服务器：{server}（ivyea mcp list 查看 / ivyea mcp add 添加）"
    client = None
    try:
        client = MCPClient(spec)
        client.initialize()
        return fn(client), None
    except MCPError as e:
        return None, f"MCP 错误（{server}）：{e}"
    except Exception as e:  # noqa: BLE001
        return None, f"MCP 调用出错（{server}）：{e}"
    finally:
        if client is not None:
            client.close()


def _mcp_targets(server: str):
    servers = _mcp_servers()
    if not servers:
        return None, "未配置任何 MCP 服务器（ivyea mcp add 添加）。"
    return ([server] if server else list(servers)), None


def _render_mcp_content(res) -> str:
    if not isinstance(res, dict):
        return json.dumps(res, ensure_ascii=False) if res else "（空结果）"
    parts = []
    for block in res.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    text = "\n".join(p for p in parts if p) or json.dumps(res, ensure_ascii=False)
    return ("[工具返回错误]\n" + text) if res.get("isError") else text


def t_mcp_list_tools(args: dict, ctx) -> str:
    """列出已配置 MCP 服务器的可用工具（只读）。server 省略=全部。"""
    targets, err = _mcp_targets((args.get("server") or "").strip())
    if err:
        return err
    out = []
    for s in targets:
        res, e = _mcp_run(s, lambda c: c.list_tools())
        if e:
            out.append(f"[{s}] {e}")
            continue
        lines = [f"[{s}] {len(res)} 个工具："]
        lines += [f"  · {t.get('name')} — {(t.get('description') or '')[:80]}" for t in res]
        out.append("\n".join(lines))
    return _truncate("\n".join(out))


def t_mcp_call_tool(args: dict, ctx) -> str:
    """调用某个 MCP 服务器的工具。计划模式拒绝；trusted 服务器免审，否则人工审批。"""
    server = (args.get("server") or "").strip()
    tool = (args.get("tool") or "").strip()
    if not server or not tool:
        return "需要 server 和 tool。先用 mcp_list_tools 查看可用工具。"
    spec = _mcp_servers().get(server)
    if not spec:
        return f"未配置 MCP 服务器：{server}（ivyea mcp list / add）"
    arguments = args.get("arguments") or {}
    if getattr(ctx, "plan_mode", False):
        return f"计划模式（只读）：不调用 MCP 工具 {server}.{tool}。/approve 后再做。"
    if not spec.get("trusted"):
        preview = f"调用 MCP 工具 {server}.{tool}　参数：{json.dumps(arguments, ensure_ascii=False)[:300]}"
        decision = permission.request_intent({"op_type": "mcp_call_tool"}, preview, ctx.perm)
        if decision == permission.ABORT:
            return "用户终止。"
        if decision != permission.APPROVE:
            return f"已跳过：{preview}"
    res, e = _mcp_run(server, lambda c: c.call_tool(tool, arguments))
    return e if e else _truncate(_render_mcp_content(res))


def t_mcp_list_resources(args: dict, ctx) -> str:
    """列出 MCP 服务器的资源（只读）。server 省略=全部。"""
    targets, err = _mcp_targets((args.get("server") or "").strip())
    if err:
        return err
    out = []
    for s in targets:
        res, e = _mcp_run(s, lambda c: c.list_resources())
        if e:
            out.append(f"[{s}] {e}")
            continue
        lines = [f"[{s}] {len(res)} 个资源："]
        lines += [f"  · {r.get('uri')} — {(r.get('name') or r.get('description') or '')[:80]}" for r in res]
        out.append("\n".join(lines))
    return _truncate("\n".join(out))


def t_mcp_read_resource(args: dict, ctx) -> str:
    """读取 MCP 服务器某个资源的内容（只读）。"""
    server = (args.get("server") or "").strip()
    uri = (args.get("uri") or "").strip()
    if not server or not uri:
        return "需要 server 和 uri。先用 mcp_list_resources 查看。"
    res, e = _mcp_run(server, lambda c: c.read_resource(uri))
    if e:
        return e
    parts = [(c.get("text") or c.get("blob") or json.dumps(c, ensure_ascii=False))
             for c in (res or []) if isinstance(c, dict)]
    return _truncate("\n".join(p for p in parts if p) or "（空）")


def t_mcp_list_prompts(args: dict, ctx) -> str:
    """列出 MCP 服务器提供的 prompt 模板（只读）。server 省略=全部。"""
    targets, err = _mcp_targets((args.get("server") or "").strip())
    if err:
        return err
    out = []
    for s in targets:
        res, e = _mcp_run(s, lambda c: c.list_prompts())
        if e:
            out.append(f"[{s}] {e}")
            continue
        lines = [f"[{s}] {len(res)} 个 prompt："]
        lines += [f"  · {p.get('name')} — {(p.get('description') or '')[:80]}" for p in res]
        out.append("\n".join(lines))
    return _truncate("\n".join(out))


def t_mcp_get_prompt(args: dict, ctx) -> str:
    """获取 MCP 服务器某个 prompt 模板的内容（只读）。"""
    server = (args.get("server") or "").strip()
    name = (args.get("name") or "").strip()
    if not server or not name:
        return "需要 server 和 name。先用 mcp_list_prompts 查看。"
    res, e = _mcp_run(server, lambda c: c.get_prompt(name, args.get("arguments") or {}))
    if e:
        return e
    parts = []
    if isinstance(res, dict):
        if res.get("description"):
            parts.append(str(res["description"]))
        for m in res.get("messages") or []:
            content = m.get("content") if isinstance(m, dict) else None
            txt = content.get("text") if isinstance(content, dict) else (content if isinstance(content, str) else "")
            parts.append(f"[{m.get('role', '?')}] {txt}")
    return _truncate("\n".join(p for p in parts if p) or json.dumps(res, ensure_ascii=False))


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
    preview = f"写文件 {_disp(p)}（{'覆盖' if exists else '新建'}，{len(content)} 字）"
    if exists:
        try:
            preview += "\n" + panels.render_diff(p.read_text(encoding="utf-8"), content, p.name)
        except Exception:
            pass
    ok, msg = _gate(ctx, "write_file", preview, detail={"path": str(p)})
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
    if cnt == 0 and _LINE_NO_RE.search(old):
        old = _strip_line_no(old)   # 容错：模型把 read_file 的行号粘进了 old
        cnt = text.count(old)
    if cnt == 0:
        return "未找到要替换的原文（old 不匹配）。注意 old 用文件真实内容，不要带 read_file 的行号。"
    if cnt > 1:
        return f"原文出现 {cnt} 次，不唯一；请提供更长的 old 以唯一定位。"
    blocked = _require_read(ctx, [str(p)])
    if blocked:
        return blocked
    preview = f"编辑 {_disp(p)}：替换 1 处\n" + panels.render_diff(old, new, p.name)
    ok, msg = _gate(ctx, "edit_file", preview, detail={"path": str(p)})
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


_SPILL_SEQ = [0]


def _spill_output(ctx, out: str) -> str:
    """超长命令输出全量落盘（先脱敏），返回路径；失败返回空串。
    按会话分目录存 ~/.ivyea/outputs/<session>/，暂无自动清理（体量小，后续可入 doctor）。"""
    try:
        import time as _t
        sess = getattr(ctx, "session_id", "") or "nosession"
        d = config.IVYEA_DIR / "outputs" / sess
        d.mkdir(parents=True, exist_ok=True)
        _SPILL_SEQ[0] += 1
        p = d / f"{_t.strftime('%Y%m%d-%H%M%S')}-{_SPILL_SEQ[0]:03d}.txt"
        p.write_text(security.redact_text(out), encoding="utf-8")
        return str(p)
    except Exception:  # noqa: BLE001
        return ""


def _run(cmd, args, ctx, kind: str, preview: str, *, auto_ok: bool = False,
         detail: dict | None = None) -> str:
    if not auto_ok:   # 只读命令(auto_ok)免审批，也可在计划模式下跑（本就只读）
        ok, msg = _gate(ctx, kind, preview, detail=detail)
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
    if not out:
        return head + "（无输出）"
    body = _truncate(out)
    if len(out) > _MAX_OUT:   # 被截断：全量落盘，模型可 read_file 续读剩余部分
        saved = _spill_output(ctx, out)
        if saved:
            body += f"\n（完整输出已保存：{saved}，需要剩余部分时用 read_file 带 offset/limit 读该文件）"
    return head + body


# ── 后台/长任务 bash：非阻塞 Popen + 输出轮询（对标 Claude Code 的后台 bash）──
_BG_PROCS: dict = {}   # bash_id -> {proc, logpath, cmd, read_pos, started}
_BG_SEQ = [0]


def _run_background(cmd, args, ctx, preview: str, *, auto_ok: bool,
                    detail: dict | None = None) -> str:
    """在后台起进程，立即返回 bash_id；输出写临时日志，供 bash_output 轮询。"""
    if not auto_ok:
        ok, msg = _gate(ctx, "run_command", preview, detail=detail)
        if not ok:
            return msg
    workdir = getattr(ctx, "workspace", "") or os.getcwd()
    import tempfile
    _BG_SEQ[0] += 1
    bash_id = f"bg-{_BG_SEQ[0]}"
    logf = tempfile.NamedTemporaryFile(prefix=f"ivyea-{bash_id}-", suffix=".log", delete=False)
    try:
        proc = subprocess.Popen(cmd, cwd=workdir, stdout=logf, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                preexec_fn=_make_preexec(0) if os.name != "nt" else None)
    except Exception as e:  # noqa: BLE001
        logf.close()
        return f"后台启动失败：{e}"
    logf.close()
    _BG_PROCS[bash_id] = {"proc": proc, "logpath": logf.name,
                          "cmd": (args.get("command") or "")[:200], "read_pos": 0}
    return (f"已在后台启动：bash_id={bash_id}（pid {proc.pid}）。"
            f"用 bash_output(bash_id=\"{bash_id}\") 查看输出/状态，kill_bash 终止。")


def t_bash_output(args: dict, ctx) -> str:
    """读取某后台 bash 自上次以来的新增输出 + 运行状态（只读，自动放行）。"""
    bash_id = str(args.get("bash_id") or "").strip()
    rec = _BG_PROCS.get(bash_id)
    if not rec:
        running = ", ".join(k for k, v in _BG_PROCS.items() if v["proc"].poll() is None)
        return f"没有该后台任务：{bash_id}。" + (f"运行中的：{running}" if running else "当前无运行中的后台任务。")
    proc = rec["proc"]
    try:
        with open(rec["logpath"], "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(rec["read_pos"])
            chunk = fh.read()
            rec["read_pos"] = fh.tell()
    except Exception as e:  # noqa: BLE001
        chunk = f"(读日志失败：{e})"
    code = proc.poll()
    status = "运行中" if code is None else f"已结束（退出码 {code}）"
    body = _truncate(chunk) if chunk else "（无新增输出）"
    return f"[{bash_id} · {status}]\n{body}"


def t_kill_bash(args: dict, ctx) -> str:
    """终止某后台 bash（写操作，需审批）。"""
    bash_id = str(args.get("bash_id") or "").strip()
    rec = _BG_PROCS.get(bash_id)
    if not rec:
        return f"没有该后台任务：{bash_id}。"
    ok, msg = _gate(ctx, "run_command", f"终止后台任务 {bash_id}：{rec['cmd']}")
    if not ok:
        return msg
    proc = rec["proc"]
    if proc.poll() is not None:
        return f"{bash_id} 已经结束（退出码 {proc.poll()}）。"
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:  # noqa: BLE001
        return f"终止失败：{e}"
    return f"已终止 {bash_id}。"


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
    auto_ok = policy.is_readonly_command(command)   # 只读命令自动放行，省去逐次审批
    preview = "运行命令：" + _truncate(command, 400)
    if args.get("run_in_background"):   # 长任务(dev server/watch/构建)：后台非阻塞，返回 bash_id
        return _run_background(shell, args, ctx, preview, auto_ok=auto_ok, detail={"command": command})
    return _run(shell, args, ctx, "run_command", preview, auto_ok=auto_ok, detail={"command": command})


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
    if not clean:
        return "计划已清空。"
    done = sum(1 for t in clean if t["status"] == "completed")
    running = sum(1 for t in clean if t["status"] == "in_progress")
    msg = f"已更新计划：{done}/{len(clean)} 完成。"
    if running > 1:   # 纪律提醒：同一时间应恰好一个进行中
        msg += f"（注意：有 {running} 个 in_progress，建议同一时间只保留一个进行中）"
    elif running == 0 and done < len(clean):
        msg += "（还有未完成步骤，记得把下一步标 in_progress）"
    return msg


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


def t_self_critique(args: dict, ctx) -> str:
    """收尾前对自己的草稿答案做一次 rubric 自查（只读，不写）。用当前主脑复核。"""
    from . import critique as _crit
    draft = (args.get("draft") or "").strip()
    if not draft:
        return "draft 为空：把你准备交付的最终答案放进 draft 再自查。"
    provider = getattr(ctx, "provider", None)
    res = _crit.critique(args.get("task") or "", draft, provider)
    if not res.get("ok"):
        return res.get("note") or "自我批判不可用。"
    return _truncate(res["markdown"] or "未见明显问题。")


# ── schema + dispatch ────────────────────────────────────────────────────────
def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


GENERAL_TOOL_SCHEMAS = [
    _fn("read_file", "读取本地文本文件内容（只读，自动放行）。返回带行号（行号\\t内容，仅供定位/引用，"
        "edit_file 的 old 用真实内容不要带行号）。大文件用 offset/limit 读行区间，别用 run_command 分段读。",
        {"path": {"type": "string", "description": "文件路径，支持 ~"},
         "offset": {"type": "integer", "description": "起始行号（从 1 开始）；读大文件某段时填"},
         "limit": {"type": "integer", "description": "最多读多少行；配合 offset 读区间"}}, ["path"]),
    _fn("list_dir", "列出目录内容（只读）。",
        {"path": {"type": "string", "description": "目录路径，默认当前目录"}}),
    _fn("write_file", "新建或整体重写文件（写操作，一次调用即审批落盘）。改已有文件的某一处别用它，用 edit_file。",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("edit_file", "改单个文件的某一处：唯一字符串替换 old→new（写操作，一次调用即审批落盘）。"
        "old 用文件真实内容（不要带 read_file 的行号前缀）且必须唯一出现。"
        "单处改动首选它；跨多文件/多处或要顺带跑测试用 code_apply_patch。",
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        ["path", "old", "new"]),
    _fn("run_python", "在沙箱(限工作目录/超时)运行 Python 代码取 stdout（执行，会弹审批）。可用 pandas/openpyxl 等。",
        {"code": {"type": "string"}, "timeout": {"type": "integer", "description": "秒，默认30"}}, ["code"]),
    _fn("run_command", "在沙箱运行 shell 命令取输出（执行，会弹审批）。长任务（dev server/watch/"
        "长构建等不会很快结束的命令）设 run_in_background=true 后台运行，立即返回 bash_id，再用 "
        "bash_output 轮询输出，别在前台阻塞等待。",
        {"command": {"type": "string"}, "timeout": {"type": "integer"},
         "run_in_background": {"type": "boolean", "description": "true=后台非阻塞运行，返回 bash_id"}},
        ["command"]),
    _fn("bash_output", "读取某后台 bash 自上次以来的新增输出与运行状态（只读，自动放行）。配合 "
        "run_command(run_in_background=true) 轮询长任务进展。",
        {"bash_id": {"type": "string", "description": "run_command 后台返回的 bash_id"}}, ["bash_id"]),
    _fn("kill_bash", "终止某个后台 bash 任务（写操作，会弹审批）。",
        {"bash_id": {"type": "string"}}, ["bash_id"]),
    _fn("web_fetch", "抓取一个 URL 的文本内容（GET，只读，自动放行）。",
        {"url": {"type": "string"}}, ["url"]),
    _fn("web_search", "网页搜索关键词（尽力而为，无 key）。",
        {"query": {"type": "string"}}, ["query"]),
    _fn("grep", "在代码库里做内容正则搜索（ripgrep 风格），返回 file:line 命中行。只读，自动放行。找代码先用它，别瞎猜路径。",
        {"pattern": {"type": "string", "description": "正则表达式"},
         "glob": {"type": "string", "description": "可选：只搜匹配此 glob 的文件，如 *.py"},
         "max_results": {"type": "integer", "description": "最多命中条数，默认 80"}}, ["pattern"]),
    _fn("glob", "按文件名 glob 模式找文件（如 **/*.py、src/**/*.ts）。只读，自动放行。"
        "想按文件名/路径定位文件用它；想按内容搜用 grep。",
        {"pattern": {"type": "string", "description": "glob 模式，如 **/*.py"},
         "path": {"type": "string", "description": "起始目录，默认工作目录"},
         "max_results": {"type": "integer", "description": "最多返回条数，默认 100"}}, ["pattern"]),
    _fn("code_search", "按符号/路径/预览检索代码库里最相关的文件（索引搜索）。只读。适合“这功能在哪实现的”。",
        {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    _fn("code_symbols", "列出/搜索代码库里的函数/类等符号定义位置。只读。",
        {"query": {"type": "string", "description": "可选，过滤符号名"}, "limit": {"type": "integer"}}),
    _fn("code_impact", "查某个符号或文件的调用方、导入方与受影响测试（改动影响面）。只读。改代码前先看它。",
        {"target": {"type": "string", "description": "符号名或文件路径"}, "limit": {"type": "integer"}}, ["target"]),
    _fn("code_apply_patch", "一次性应用一组结构化补丁并跑测试（跨多文件/多处关联改动，或要顺带跑测试时用它）。"
        "内部先校验→给彩色 diff 预览→一次人工审批→落盘+跑测试，不需要分 dry-run/execute 两步。"
        "ops=[{path,old,new}]，old 必须在文件中唯一出现。单文件单处改动用 edit_file 即可。",
        {"ops": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}}},
         "test_command": {"type": "string", "description": "可选：落盘后跑的测试命令，默认自动挑选"}}, ["ops"]),
    _fn("run_tests", "在工作目录跑测试命令并返回结果（执行，会弹审批）。默认 python -m pytest。",
        {"command": {"type": "string"}, "timeout": {"type": "integer"}}),
    _fn("code_repair", "解析失败测试输出，生成下一轮修复计划（可疑文件/失败摘要/重跑命令）。只读。",
        {"test_output": {"type": "string"}}, ["test_output"]),
    _fn("mcp_list_tools", "列出已配置 MCP 服务器的可用工具（只读）。server 省略=全部。先用它发现工具，再 mcp_call_tool。",
        {"server": {"type": "string", "description": "MCP 服务器名（mcp.json 配置）；省略=全部"}}),
    _fn("mcp_call_tool", "调用某个 MCP 服务器的工具。会弹人工审批（mcp.json 标 trusted 的服务器免审）；计划模式下拒绝。",
        {"server": {"type": "string"}, "tool": {"type": "string"},
         "arguments": {"type": "object", "description": "传给工具的 JSON 参数"}}, ["server", "tool"]),
    _fn("mcp_list_resources", "列出 MCP 服务器的资源（只读）。server 省略=全部。",
        {"server": {"type": "string"}}),
    _fn("mcp_read_resource", "读取 MCP 服务器某个资源的内容（只读）。",
        {"server": {"type": "string"}, "uri": {"type": "string"}}, ["server", "uri"]),
    _fn("mcp_list_prompts", "列出 MCP 服务器提供的 prompt 模板（只读）。server 省略=全部。",
        {"server": {"type": "string"}}),
    _fn("mcp_get_prompt", "获取 MCP 服务器某个 prompt 模板的内容（只读）。",
        {"server": {"type": "string"}, "name": {"type": "string"},
         "arguments": {"type": "object", "description": "模板参数（可选）"}}, ["server", "name"]),
    _fn("todo_write", "维护多步任务计划(让长任务可视化)。每步 {content, status: pending|in_progress|completed}。多步任务动手前先列计划；执行时同一时间恰好一个 in_progress，完成一步立刻标 completed 再开下一步，发现新子步骤就追加。单步小任务不必用。",
        {"todos": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}}}},
        ["todos"]),
    _fn("self_critique", "收尾前自查：把你准备交付的最终答案放进 draft，用当前主脑按 rubric 复核"
        "(需求吻合/事实可靠/关键遗漏/验证到位)，返回简短批判。高风险或复杂任务交付前建议先自调一次。只读。",
        {"draft": {"type": "string", "description": "准备交付给用户的最终答案全文"},
         "task": {"type": "string", "description": "可选：本次任务/需求，帮助判断是否答非所问"}},
        ["draft"]),
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
    "bash_output": t_bash_output, "kill_bash": t_kill_bash,
    "web_fetch": t_web_fetch, "web_search": t_web_search,
    "grep": t_grep, "glob": t_glob, "code_search": t_code_search,
    "code_symbols": t_code_symbols, "code_impact": t_code_impact,
    "code_apply_patch": t_code_apply_patch, "run_tests": t_run_tests, "code_repair": t_code_repair,
    "mcp_list_tools": t_mcp_list_tools, "mcp_call_tool": t_mcp_call_tool,
    "mcp_list_resources": t_mcp_list_resources, "mcp_read_resource": t_mcp_read_resource,
    "mcp_list_prompts": t_mcp_list_prompts, "mcp_get_prompt": t_mcp_get_prompt,
    "todo_write": t_todo_write,
    "self_critique": t_self_critique,
    "task_read": t_task_read,
    "task_step": t_task_step,
    "task_log": t_task_log,
    "task_resume": t_task_resume,
}
