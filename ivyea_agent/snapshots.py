"""快照差分 —— L1 实时层的基础设施。

许多"紧急异常"不是某个数值越界，而是**状态发生了跃迁**：活动昨天还在投今天暂停了、
预算被人从 300 改成 30、不可售库存突然涨了。这类只能靠比对上一次快照发现。

顺带提供一个副产品：「谁动了我的广告」——把外部改动和 agent 自己的写操作区分开
（后者在 audit 里有记录）。

存 ~/.ivyea/snapshots.db。

**冷启动纪律**：第一次跑某个 (sid, kind) 时没有基线，此时**一条差分都不能报**，
否则用户第一次运行就会收到几百条"变更"告警。``diff()`` 用 ``first_run`` 显式表达这件事。
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import config

_DB = config.IVYEA_DIR / "snapshots.db"


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    c = sqlite3.connect(str(_DB))
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        sid TEXT NOT NULL,
        kind TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        ts REAL NOT NULL,
        PRIMARY KEY (sid, kind, entity_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS snapshot_runs (
        sid TEXT NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL,
        row_count INTEGER NOT NULL,
        PRIMARY KEY (sid, kind))""")
    return c


@dataclass(frozen=True)
class Change:
    """一处变更。``kind`` = added | removed | changed。"""
    entity_id: str
    change: str
    field: str = ""
    before: Any = None
    after: Any = None
    row: Optional[dict[str, Any]] = None
    prev_row: Optional[dict[str, Any]] = None

    def describe(self) -> str:
        if self.change == "added":
            return f"新增 {self.entity_id}"
        if self.change == "removed":
            return f"消失 {self.entity_id}"
        return f"{self.entity_id} 的 {self.field}：{self.before} → {self.after}"


@dataclass(frozen=True)
class DiffResult:
    first_run: bool
    changes: list[Change]
    previous_ts: float = 0.0
    row_count: int = 0

    @property
    def has_baseline(self) -> bool:
        return not self.first_run


def load(sid: Any, kind: str) -> dict[str, dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT entity_id, payload FROM snapshots WHERE sid=? AND kind=?",
            (str(sid), kind)).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            out[r["entity_id"]] = json.loads(r["payload"])
        except json.JSONDecodeError:
            continue        # 单行损坏不该拖垮整次巡检
    return out


def has_baseline(sid: Any, kind: str) -> bool:
    conn = _conn()
    try:
        row = conn.execute("SELECT 1 FROM snapshot_runs WHERE sid=? AND kind=?",
                           (str(sid), kind)).fetchone()
    finally:
        conn.close()
    return row is not None


def save(sid: Any, kind: str, rows: Iterable[dict[str, Any]], key_field: str) -> int:
    """整体替换该 (sid, kind) 的快照。返回写入行数。"""
    sid = str(sid)
    now = time.time()
    payload = []
    for r in rows:
        key = str(r.get(key_field) or "")
        if not key:
            continue
        payload.append((sid, kind, key, json.dumps(r, ensure_ascii=False), now))
    conn = _conn()
    try:
        conn.execute("DELETE FROM snapshots WHERE sid=? AND kind=?", (sid, kind))
        conn.executemany(
            "INSERT OR REPLACE INTO snapshots (sid,kind,entity_id,payload,ts) VALUES (?,?,?,?,?)",
            payload)
        conn.execute(
            "INSERT OR REPLACE INTO snapshot_runs (sid,kind,ts,row_count) VALUES (?,?,?,?)",
            (sid, kind, now, len(payload)))
        conn.commit()
    finally:
        conn.close()
    return len(payload)


def diff(sid: Any, kind: str, rows: list[dict[str, Any]], key_field: str,
         watch_fields: Iterable[str], *, track_membership: bool = True) -> DiffResult:
    """与上次快照比对。**不写入**——写入由 ``save()`` 显式完成，便于测试与失败重试。"""
    watch = tuple(watch_fields)
    first = not has_baseline(sid, kind)
    prev = load(sid, kind)
    changes: list[Change] = []

    if first:
        # 冷启动：建立基线，一条都不报。
        return DiffResult(first_run=True, changes=[], row_count=len(rows))

    seen: set[str] = set()
    for r in rows:
        key = str(r.get(key_field) or "")
        if not key:
            continue
        seen.add(key)
        old = prev.get(key)
        if old is None:
            if track_membership:
                changes.append(Change(key, "added", row=r))
            continue
        for f in watch:
            before, after = old.get(f), r.get(f)
            if before != after:
                changes.append(Change(key, "changed", field=f, before=before,
                                      after=after, row=r, prev_row=old))
    if track_membership:
        for key, old in prev.items():
            if key not in seen:
                changes.append(Change(key, "removed", prev_row=old))

    prev_ts = 0.0
    conn = _conn()
    try:
        row = conn.execute("SELECT ts FROM snapshot_runs WHERE sid=? AND kind=?",
                           (str(sid), kind)).fetchone()
        if row:
            prev_ts = float(row["ts"])
    finally:
        conn.close()
    return DiffResult(first_run=False, changes=changes,
                      previous_ts=prev_ts, row_count=len(rows))


def clear(sid: Any = "", kind: str = "") -> int:
    """清快照（测试与重置用）。不传参数清全部。"""
    conn = _conn()
    try:
        where, args = [], []
        if sid:
            where.append("sid=?"); args.append(str(sid))
        if kind:
            where.append("kind=?"); args.append(kind)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        n = conn.execute(f"DELETE FROM snapshots{clause}", args).rowcount
        conn.execute(f"DELETE FROM snapshot_runs{clause}", args)
        conn.commit()
    finally:
        conn.close()
    return n
