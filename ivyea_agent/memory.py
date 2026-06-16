"""运营记忆（Hermes 同款：SQLite + FTS5 + 自策展；本地自有，不依赖 GBrain/向量库）。

存 ~/.ivyea/memory.db：
- decisions：每个 ASIN+词+动作的人工裁决(approve/reject)与时间 → 支撑"尊重历史否决"
  和"5 天稳定期"。
- runs：每次巡检记录。
- search_fts：FTS5 全文检索(跨会话回忆)；FTS5 不可用时降级到普通表 + LIKE。

策展 markdown（MEMORY.md / account/<ASIN>.md）由 memory_md 提供。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

from . import config

DB_PATH = config.IVYEA_DIR / "memory.db"
_FTS_OK: Optional[bool] = None


def _detect_fts(conn: sqlite3.Connection) -> bool:
    global _FTS_OK
    if _FTS_OK is None:
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(text, asin, ts UNINDEXED)")
            _FTS_OK = True
        except Exception:
            conn.execute("CREATE TABLE IF NOT EXISTS search_fts (text TEXT, asin TEXT, ts REAL)")
            _FTS_OK = False
    return _FTS_OK


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asin TEXT, term TEXT, kind TEXT, decision TEXT, ts REAL, note TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asin TEXT, ts REAL, negatives INTEGER, scale INTEGER, reduce INTEGER, note TEXT)""")
    _detect_fts(conn)
    conn.commit()
    return conn


def _index(conn: sqlite3.Connection, text: str, asin: str, ts: float) -> None:
    conn.execute("INSERT INTO search_fts (text, asin, ts) VALUES (?, ?, ?)", (text, asin, ts))


def record_decision(asin: str, term: str, kind: str, decision: str, note: str = "") -> None:
    """decision: approve | reject。"""
    ts = time.time()
    conn = _conn()
    conn.execute("INSERT INTO decisions (asin, term, kind, decision, ts, note) VALUES (?,?,?,?,?,?)",
                 (asin or "", term, kind, decision, ts, note))
    _index(conn, f"[决策] {decision} {kind} “{term}” {note}", asin or "", ts)
    conn.commit(); conn.close()


def record_run(asin: str, negatives: int = 0, scale: int = 0, reduce: int = 0, note: str = "") -> None:
    ts = time.time()
    conn = _conn()
    conn.execute("INSERT INTO runs (asin, ts, negatives, scale, reduce, note) VALUES (?,?,?,?,?,?)",
                 (asin or "", ts, negatives, scale, reduce, note))
    _index(conn, f"[巡检] {asin} 否词{negatives}/放量{scale}/降bid{reduce} {note}", asin or "", ts)
    conn.commit(); conn.close()


def was_rejected(asin: str, term: str, kind: str) -> bool:
    """该 ASIN+词+动作 最近一次人工裁决是否为 reject。"""
    conn = _conn()
    row = conn.execute(
        "SELECT decision FROM decisions WHERE asin=? AND term=? AND kind=? ORDER BY ts DESC LIMIT 1",
        (asin or "", term, kind)).fetchone()
    conn.close()
    return bool(row and row["decision"] == "reject")


def days_since_last_approve(asin: str, term: str, kinds: tuple = ("reduce_bid", "scale_up")) -> Optional[float]:
    """该 ASIN+词 最近一次被批准执行(调价类)距今天数；无则 None。"""
    conn = _conn()
    qs = ",".join("?" * len(kinds))
    row = conn.execute(
        f"SELECT ts FROM decisions WHERE asin=? AND term=? AND decision='approve' AND kind IN ({qs}) "
        "ORDER BY ts DESC LIMIT 1", (asin or "", term, *kinds)).fetchone()
    conn.close()
    return (time.time() - row["ts"]) / 86400.0 if row else None


def recent_runs(asin: str = "", limit: int = 5) -> list[dict[str, Any]]:
    conn = _conn()
    if asin:
        rows = conn.execute("SELECT * FROM runs WHERE asin=? ORDER BY ts DESC LIMIT ?", (asin, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        if _FTS_OK:
            rows = conn.execute("SELECT text, asin, ts FROM search_fts WHERE search_fts MATCH ? "
                                "ORDER BY rank LIMIT ?", (query, limit)).fetchall()
        else:
            rows = conn.execute("SELECT text, asin, ts FROM search_fts WHERE text LIKE ? "
                                "ORDER BY ts DESC LIMIT ?", (f"%{query}%", limit)).fetchall()
    except Exception:
        rows = conn.execute("SELECT text, asin, ts FROM search_fts WHERE text LIKE ? "
                            "ORDER BY ts DESC LIMIT ?", (f"%{query}%", limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats() -> dict[str, Any]:
    conn = _conn()
    d = conn.execute("SELECT COUNT(*) c, SUM(decision='approve') a, SUM(decision='reject') r FROM decisions").fetchone()
    n = conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
    conn.close()
    return {"decisions": d["c"] or 0, "approved": d["a"] or 0, "rejected": d["r"] or 0,
            "runs": n, "fts": _FTS_OK, "db": str(DB_PATH)}


def note_path(asin: str = ""):
    if asin:
        d = config.IVYEA_DIR / "account"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{asin}.md"
    return config.IVYEA_DIR / "MEMORY.md"


def read_note(asin: str = "") -> str:
    p = note_path(asin)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def remember(text: str, asin: str = "") -> str:
    """把一条要点追加到策展 markdown（MEMORY.md 或 account/<asin>.md）并入检索。"""
    text = (text or "").strip()
    if not text:
        return "（空，未记）"
    p = note_path(asin)
    ts = time.strftime("%Y-%m-%d %H:%M")
    head = "" if p.exists() else (f"# {asin} 运营记忆\n\n" if asin else "# Ivyea Agent 记忆\n\n")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"{head}- [{ts}] {text}\n")
    conn = _conn()
    _index(conn, f"[记忆] {asin} {text}", asin or "", time.time())
    conn.commit(); conn.close()
    return f"已记到 {p.name}"


def annotate(actions: list, asin: str, stability_days: int = 5) -> list:
    """记忆护栏：把"历史已否决/稳定期内"的动作标记为 blocked（叠加在硬护栏之上）。"""
    if not asin:
        return actions
    for a in actions:
        if a.blocked:
            continue
        if was_rejected(asin, a.search_term, a.kind):
            a.blocked, a.block_reason = True, "记忆：上次人工已否决，不再自动建议（如需可手动执行）"
        elif a.kind in ("reduce_bid", "scale_up"):
            d = days_since_last_approve(asin, a.search_term)
            if d is not None and d < stability_days:
                a.blocked, a.block_reason = True, f"记忆：{stability_days} 天稳定期内（{d:.1f} 天前刚调过），不重复调"
    return actions
