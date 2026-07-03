"""Local run timeline for tool calls and agent events."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from . import config, security

DB_PATH = config.IVYEA_DIR / "traces.db"


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    # timeout=5：并行子 agent/只读工具多线程各自写时等文件锁，而非立刻 database is locked
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        turn_id TEXT,
        event TEXT,
        name TEXT,
        ok INTEGER,
        duration_ms INTEGER,
        summary TEXT,
        payload TEXT,
        ts REAL
    )""")
    conn.commit()
    return conn


def record(session_id: str, turn_id: str, event: str, name: str = "",
           ok: bool = True, duration_ms: int = 0, summary: str = "",
           payload: dict[str, Any] | None = None) -> None:
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO events (session_id, turn_id, event, name, ok, duration_ms, summary, payload, ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id or "", turn_id or "", event, name, 1 if ok else 0, int(duration_ms or 0),
             security.redact_text(summary)[:1000], json.dumps(security.redact_obj(payload or {}), ensure_ascii=False),
             time.time()),
        )
        conn.commit()
        conn.close()
    except sqlite3.OperationalError:
        return   # trace 是 best-effort：极端并发下锁等待超时宁可丢一条也不炸工具调用


def recent(limit: int = 20, session_id: str = "") -> list[dict[str, Any]]:
    conn = _conn()
    if session_id:
        rows = conn.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats(limit: int = 1000) -> dict[str, Any]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    tool_rows = [r for r in rows if r["event"] == "tool_call"]
    failures = [r for r in rows if not r["ok"]]
    return {
        "db": str(DB_PATH),
        "events": len(rows),
        "tool_calls": len(tool_rows),
        "failures": len(failures),
        "avg_tool_ms": int(sum(r["duration_ms"] for r in tool_rows) / len(tool_rows)) if tool_rows else 0,
    }


def render_recent(limit: int = 20, session_id: str = "") -> str:
    rows = recent(limit=limit, session_id=session_id)
    if not rows:
        return "（暂无运行时间线）"
    lines = []
    for r in rows:
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
        ok = "ok" if r["ok"] else "fail"
        name = f" {r['name']}" if r["name"] else ""
        dur = f" {r['duration_ms']}ms" if r["duration_ms"] else ""
        lines.append(f"{ts} {r['event']}{name} {ok}{dur} · {r['summary']}")
    return "\n".join(lines)
