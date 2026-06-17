"""通用工具层 —— 让 agent 不止会广告巡检，能干真实运营活。

读类（read_file/list_dir/web_fetch/web_search）自动放行；写/执行类
（write_file/edit_file/run_python/run_command）经人工审批门控（复用 permission），
且在计划模式下一律拒绝。执行类带沙箱：限工作目录、超时、输出截断。

设计取自 Claude API agent-design：读广、写/执行经门控（可审计、可拦截）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from . import permission

_MAX_OUT = 4000          # 工具返回截断（防爆上下文）
_EXEC_TIMEOUT = 30       # 执行类默认超时（秒）
_MUTATING = {"write_file", "edit_file", "run_python", "run_command"}


def _truncate(s: str, n: int = _MAX_OUT) -> str:
    return s if len(s) <= n else s[:n] + f"\n…（已截断，共 {len(s)} 字）"


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


# ── 写/执行类（门控）─────────────────────────────────────────────────────────
def t_write_file(args: dict, ctx) -> str:
    path = os.path.expanduser(args.get("path", ""))
    content = args.get("content", "")
    if not path:
        return "path 为空。"
    p = Path(path).resolve()
    exists = p.exists()
    ok, msg = _gate(ctx, "write_file",
                    f"写文件 {p}（{'覆盖' if exists else '新建'}，{len(content)} 字）")
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
    ok, msg = _gate(ctx, "edit_file", f"编辑 {p}：替换 1 处（{len(old)}→{len(new)} 字）")
    if not ok:
        return msg
    try:
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"已编辑 {p}（替换 1 处）"
    except Exception as e:  # noqa: BLE001
        return f"编辑失败：{e}"


def _run(cmd, args, ctx, kind: str, preview: str) -> str:
    ok, msg = _gate(ctx, kind, preview)
    if not ok:
        return msg
    workdir = getattr(ctx, "workspace", "") or os.getcwd()
    timeout = int(args.get("timeout") or _EXEC_TIMEOUT)
    try:
        proc = subprocess.run(cmd, cwd=workdir, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace")
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
    return _run(["bash", "-lc", command], args, ctx, "run_command",
                "运行命令：" + _truncate(command, 400))


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
]

GENERAL_DISPATCH = {
    "read_file": t_read_file, "list_dir": t_list_dir,
    "write_file": t_write_file, "edit_file": t_edit_file,
    "run_python": t_run_python, "run_command": t_run_command,
    "web_fetch": t_web_fetch, "web_search": t_web_search,
}
