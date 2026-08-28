"""证据台账：这一轮到底**证明**了什么。

为什么要有这一层
----------------
汇报里的"验证"一栏此前有两个来源：模型自己写的 `evidence`，和
`progress_reporting.progress_tool_evidence` 收的工具结果首行。后者已经比纯自述强得多，
但它有两个硬伤：

1. **只在 `progress_required` 的轮次收**。普通对话轮一次都不记 —— 而"你刚才到底跑没跑
   那条命令"恰恰是普通轮里最容易含糊过去的。
2. **活不过这一轮**。它挂在 `ToolContext` 上，换一轮就没了，更别说换个会话。于是
   "上次我们验证过什么"永远答不上来。

所以这里独立记一份：落盘、跨轮、跨会话、可查。它是**被动**的 —— 只记录发生过什么，
不判断够不够、不拦任何人。判断留给门禁，台账只负责"事实是什么"。

设计取舍
--------
* **单开一个 evidence.db，不挤进 traces.db**。traces 是给运行时间线 UI 看的全量流水，
  保留策略和查询形状都不一样；证据台账要按"命令/测试/读/写/接口"分类查、要能过期清理。
  两件事塞一张表，迟早互相掣肘。
* **best-effort**：写不进去一律吞掉（照抄 traces.py 的做法）。台账坏了不该连累干活。
* **只记有验证意义的工具**。grep 命中几行、list_dir 列了几项不是"证明"，全记进来
  只会把真正的证据淹掉。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import config, security

DB_PATH = config.IVYEA_DIR / "evidence.db"

#: 过期清理阈值。证据是"最近证明过什么"，半年前的没有参考价值，白占地方。
MAX_AGE_DAYS = 30
#: 每次写入有 1/N 的概率顺手清一次过期数据。没有后台任务，也不值得为它开一个。
_PRUNE_EVERY = 50

#: 工具 → 证据类别。**不在这张表里的工具一律不记** —— 它们不产生"证明"。
_TOOL_KIND = {
    "run_command": "command",
    "run_python": "command",
    "run_tests": "test",
    "read_file": "read",
    "write_file": "write",
    "edit_file": "write",
    "code_apply_patch": "write",
    "mcp_call_tool": "api",
    "ivyea_ops_call_tool": "api",
    "web_fetch": "api",
    "execute_actions": "write",
    "rollback": "write",
}

#: 从工具输出里抠退出码。三种写法都在用（tools_general / code_agent / 后台任务收尾）。
_EXIT_RE = re.compile(r"退出码\s*(-?\d+)|returncode=(-?\d+)|已结束（exit=(-?\d+)")

_CLIP = 300


def _clip(text: Any, limit: int = _CLIP) -> str:
    return " ".join(security.redact_text(str(text or "")).split())[:limit]


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        turn_id TEXT,
        kind TEXT,
        target TEXT,
        ok INTEGER,
        detail TEXT,
        ts REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_session ON evidence(session_id, turn_id)")
    conn.commit()
    return conn


def _target_for(name: str, args: dict[str, Any]) -> str:
    args = args or {}
    if name in ("run_command",):
        return _clip(args.get("command"), 200)
    if name == "run_python":
        return _clip((str(args.get("code") or "").strip().splitlines() or [""])[0], 120)
    if name == "run_tests":
        return _clip(args.get("command") or "python -m pytest", 200)
    if name in ("read_file", "write_file", "edit_file"):
        return _clip(args.get("path"), 200)
    if name == "code_apply_patch":
        paths = [str((op or {}).get("path") or "") for op in args.get("ops") or []
                 if isinstance(op, dict)]
        return _clip(", ".join(p for p in paths if p), 200)
    if name in ("mcp_call_tool",):
        return _clip(f"{args.get('server')}/{args.get('tool')}", 120)
    if name == "ivyea_ops_call_tool":
        return _clip(args.get("name"), 120)
    if name == "web_fetch":
        return _clip(args.get("url"), 200)
    return ""


def _detail_for(kind: str, ok: bool, text: str) -> str:
    body = (text or "").strip()
    if kind in ("command", "test"):
        m = _EXIT_RE.search(body)
        if m:
            code = next((g for g in m.groups() if g is not None), "")
            return f"退出码 {code}"
    if not ok:
        return _clip(body.splitlines()[0] if body else "失败", 200)
    return _clip(body.splitlines()[0] if body else "", 200)


def record_tool(session_id: str, turn_id: str, name: str, args: dict[str, Any],
                ok: bool, text: str) -> None:
    """把一次有验证意义的工具调用记进台账。不在 `_TOOL_KIND` 里的一律跳过。"""
    kind = _TOOL_KIND.get(name or "")
    if not kind:
        return
    # 被护栏拦下、或工具自己拒绝的调用不是证据，是待办。
    body = (text or "").lstrip()
    if body.startswith(("⚠", "已拦截", "已拒绝")):
        return
    target = _target_for(name, args)
    detail = _detail_for(kind, ok, text)
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO evidence (session_id, turn_id, kind, target, ok, detail, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id or "", turn_id or "", kind, target, 1 if ok else 0, detail, time.time()),
        )
        conn.commit()
        if int(time.time() * 1000) % _PRUNE_EVERY == 0:
            _prune(conn)
        conn.close()
    except sqlite3.OperationalError:
        return   # 台账是 best-effort：锁等待超时宁可丢一条，也不炸这次工具调用


def _prune(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("DELETE FROM evidence WHERE ts < ?", (time.time() - MAX_AGE_DAYS * 86400,))
        conn.commit()
    except sqlite3.OperationalError:
        return


def rows(session_id: str = "", turn_id: str = "", limit: int = 50,
         ok_only: bool = False) -> list[dict[str, Any]]:
    try:
        conn = _conn()
    except sqlite3.OperationalError:
        return []
    sql = "SELECT * FROM evidence WHERE 1=1"
    params: list[Any] = []
    if session_id:
        sql += " AND session_id=?"
        params.append(session_id)
    if turn_id:
        sql += " AND turn_id=?"
        params.append(turn_id)
    if ok_only:
        sql += " AND ok=1"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
        out = [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        out = []
    conn.close()
    return list(reversed(out))


_KIND_LABEL = {"command": "跑了命令", "test": "跑了测试", "read": "读了",
               "write": "写了", "api": "调了接口"}


def render(session_id: str = "", turn_id: str = "", limit: int = 12,
           ok_only: bool = True) -> list[str]:
    """渲染成可以直接进汇报「验证」一栏的句子。没有证据时返回空列表。"""
    out: list[str] = []
    for row in rows(session_id=session_id, turn_id=turn_id, limit=limit, ok_only=ok_only):
        label = _KIND_LABEL.get(row.get("kind") or "", row.get("kind") or "")
        target = row.get("target") or ""
        detail = row.get("detail") or ""
        mark = "" if row.get("ok") else "（失败）"
        line = f"{label} {target}{mark}".strip()
        if detail and detail not in line:
            line += f" → {detail}"
        if line and line not in out:
            out.append(line)
    return out


def has_verification(session_id: str = "", turn_id: str = "") -> bool:
    """这一轮/这个会话有没有真跑通过什么（命令、测试或接口）。"""
    return any(r.get("kind") in ("command", "test", "api")
               for r in rows(session_id=session_id, turn_id=turn_id, limit=50, ok_only=True))


def export(session_id: str, path: str | Path) -> Path:
    """导出一个会话的全部证据（排查/复盘用）。"""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rows(session_id=session_id, limit=10000),
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    return target
