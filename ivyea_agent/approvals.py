"""审批态权威存储 —— 「同意后直接执行」的骨架（ADR-5）。

为什么状态在 agent 侧而不在 relay 内存里：
1. 告警发出去，人可能两小时后才点。relay 重启不能丢待审批项。
   （hermes 的审批是阻塞式的，agent 线程卡着等，所以它能放内存；我们是异步式。）
2. 防重放：approval 一次性消费，任何非 pending 态的点击一律拒绝。
3. 溯源：谁、什么时候、在哪个会话点的，与 audit_id 关联。

存 ~T~/.ivyea/approvals.db（sqlite）。

**对方案 ADR-5 的偏离**：原方案写的是 approvals.jsonl。改用 sqlite 的原因是
"一次性消费"必须是原子的 compare-and-set —— 飞书的重复点击与网络重投是真实场景，
``UPDATE ... WHERE state='pending'`` 的 rowcount 才能保证只有一次生效，
jsonl 的读-改-写做不到。

状态机（只允许这些跃迁）：
    pending ──approve──> approved ──执行成功──> executed ──回滚──> rolled_back
       │                     └────执行失败────> failed
       ├──deny────> denied
       └──超时────> expired
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from . import config

_DB = config.IVYEA_DIR / "approvals.db"

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXECUTED = "executed"
FAILED = "failed"
ROLLED_BACK = "rolled_back"
EXPIRED = "expired"

#: 默认有效期：一天。过期的卡片再点也不执行——两天前的建议早已不适用当下数据。
DEFAULT_TTL_SECONDS = 24 * 3600

_TERMINAL = (DENIED, EXECUTED, FAILED, ROLLED_BACK, EXPIRED)


class ApprovalError(Exception):
    pass


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    c = sqlite3.connect(str(_DB), isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        sid TEXT, code TEXT, layer TEXT, action_class TEXT,
        target_id TEXT, target_name TEXT, preview TEXT,
        intent TEXT, evidence TEXT, severity TEXT,
        chat_id TEXT, message_id TEXT,
        state TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL,
        resolved_by TEXT, resolved_at REAL,
        audit_id TEXT, detail TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_appr_state ON approvals (state, expires_at)")
    return c


@dataclass
class Approval:
    id: str
    sid: str
    code: str
    layer: str
    action_class: str
    target_id: str
    target_name: str
    preview: str
    intent: Optional[dict[str, Any]]
    evidence: dict[str, Any]
    severity: str
    chat_id: str
    message_id: str
    state: str
    created_at: float
    expires_at: float
    resolved_by: str = ""
    resolved_at: float = 0.0
    audit_id: str = ""
    detail: str = ""

    @property
    def is_pending(self) -> bool:
        return self.state == PENDING

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def describe(self) -> str:
        return f"[{self.state}] {self.preview or self.code}（{self.target_name}）"


def _row_to_approval(r: sqlite3.Row) -> Approval:
    return Approval(
        id=r["id"], sid=r["sid"] or "", code=r["code"] or "", layer=r["layer"] or "",
        action_class=r["action_class"] or "", target_id=r["target_id"] or "",
        target_name=r["target_name"] or "", preview=r["preview"] or "",
        intent=json.loads(r["intent"]) if r["intent"] else None,
        evidence=json.loads(r["evidence"]) if r["evidence"] else {},
        severity=r["severity"] or "", chat_id=r["chat_id"] or "",
        message_id=r["message_id"] or "", state=r["state"],
        created_at=float(r["created_at"]), expires_at=float(r["expires_at"]),
        resolved_by=r["resolved_by"] or "", resolved_at=float(r["resolved_at"] or 0),
        audit_id=r["audit_id"] or "", detail=r["detail"] or "")


def create(finding: Any, *, chat_id: str = "", message_id: str = "",
           ttl_seconds: float = DEFAULT_TTL_SECONDS,
           preview: str = "") -> Approval:
    """由一条 Finding 创建待审批项。**没有 intent 的 Finding 不可创建**——
    纯告警不该出现「批准执行」按钮，那会让人点了以为做了事，实际什么也没发生。"""
    intent = getattr(finding, "intent", None)
    if not intent:
        raise ApprovalError("该 Finding 没有可执行 intent，不能创建审批项")

    now = time.time()
    a = Approval(
        id=uuid.uuid4().hex[:16],
        sid=str(getattr(finding, "sid", "")),
        code=str(getattr(finding, "code", "")),
        layer=str(getattr(finding, "layer", "")),
        action_class=str(getattr(finding, "action_class", "")),
        target_id=str(getattr(finding, "target_id", "")),
        target_name=str(getattr(finding, "target_name", "")),
        preview=preview or str(getattr(finding, "message", "")),
        intent=intent, evidence=dict(getattr(finding, "evidence", {}) or {}),
        severity=str(getattr(finding, "severity", "")),
        chat_id=chat_id, message_id=message_id, state=PENDING,
        created_at=now, expires_at=now + float(ttl_seconds))

    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO approvals
               (id,sid,code,layer,action_class,target_id,target_name,preview,
                intent,evidence,severity,chat_id,message_id,state,created_at,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (a.id, a.sid, a.code, a.layer, a.action_class, a.target_id, a.target_name,
             a.preview, json.dumps(a.intent, ensure_ascii=False),
             json.dumps(a.evidence, ensure_ascii=False), a.severity,
             a.chat_id, a.message_id, a.state, a.created_at, a.expires_at))
    finally:
        conn.close()
    return a


def get(approval_id: str) -> Optional[Approval]:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_approval(r) if r else None


def set_message(approval_id: str, chat_id: str, message_id: str) -> bool:
    """发卡之后回填消息 id，供后续原地更新卡片用。"""
    conn = _conn()
    try:
        n = conn.execute("UPDATE approvals SET chat_id=?, message_id=? WHERE id=?",
                         (chat_id, message_id, approval_id)).rowcount
    finally:
        conn.close()
    return n > 0


def resolve(approval_id: str, choice: str, *, operator: str = "",
            chat_id: str = "") -> tuple[bool, Optional[Approval], str]:
    """原子地把 pending 消费掉。返回 (是否成功, approval, 拒绝原因)。

    这是安全攸关的一步：飞书的重复点击、网络重投都会打到这里，
    ``UPDATE ... WHERE state='pending'`` 的 rowcount 保证只有第一次生效。
    """
    if choice not in ("approve", "deny"):
        return False, None, f"未知选择：{choice}"

    a = get(approval_id)
    if a is None:
        return False, None, "unknown"
    if a.state != PENDING:
        return False, a, "already_resolved"
    # 卡片可能被转发到别的群；发卡会话与回调会话必须一致
    if a.chat_id and chat_id and a.chat_id != chat_id:
        return False, a, "chat_mismatch"
    if a.expired:
        _force_state(approval_id, EXPIRED, detail="超过有效期未处理")
        return False, get(approval_id), "expired"

    new_state = APPROVED if choice == "approve" else DENIED
    now = time.time()
    conn = _conn()
    try:
        n = conn.execute(
            """UPDATE approvals SET state=?, resolved_by=?, resolved_at=?
               WHERE id=? AND state=?""",
            (new_state, operator, now, approval_id, PENDING)).rowcount
    finally:
        conn.close()
    if n != 1:
        return False, get(approval_id), "already_resolved"
    return True, get(approval_id), ""


def _force_state(approval_id: str, state: str, *, audit_id: str = "",
                 detail: str = "") -> bool:
    conn = _conn()
    try:
        n = conn.execute(
            "UPDATE approvals SET state=?, audit_id=COALESCE(NULLIF(?,''), audit_id), "
            "detail=? WHERE id=?",
            (state, audit_id, detail, approval_id)).rowcount
    finally:
        conn.close()
    return n > 0


def mark_executed(approval_id: str, *, audit_id: str = "", detail: str = "") -> bool:
    """只允许从 approved 跃迁到 executed —— 没批准过的不可能"执行成功"。"""
    conn = _conn()
    try:
        n = conn.execute(
            """UPDATE approvals SET state=?, audit_id=?, detail=?
               WHERE id=? AND state=?""",
            (EXECUTED, audit_id, detail, approval_id, APPROVED)).rowcount
    finally:
        conn.close()
    return n == 1


def mark_failed(approval_id: str, detail: str = "") -> bool:
    conn = _conn()
    try:
        n = conn.execute(
            """UPDATE approvals SET state=?, detail=? WHERE id=? AND state=?""",
            (FAILED, detail, approval_id, APPROVED)).rowcount
    finally:
        conn.close()
    return n == 1


def cancel(approval_id: str, detail: str = "") -> bool:
    """撤销一个尚未执行的审批（pending 或 approved）。

    批准之后、写开关补开之前改主意，是个真实场景 —— 没有这个操作的话，
    那条 intent 会一直挂着，等哪天开了写开关被 `approval execute` 捞起来执行。
    已执行的不能撤销，只能回滚。
    """
    conn = _conn()
    try:
        n = conn.execute(
            "UPDATE approvals SET state=?, detail=? WHERE id=? AND state IN (?,?)",
            (DENIED, detail or "已撤销", approval_id, PENDING, APPROVED)).rowcount
    finally:
        conn.close()
    return n == 1


def mark_rolled_back(approval_id: str, detail: str = "") -> bool:
    """只有已执行的才谈得上回滚。"""
    conn = _conn()
    try:
        n = conn.execute(
            """UPDATE approvals SET state=?, detail=? WHERE id=? AND state=?""",
            (ROLLED_BACK, detail, approval_id, EXECUTED)).rowcount
    finally:
        conn.close()
    return n == 1


def expire_due(now: Optional[float] = None) -> int:
    """把过期的 pending 标记为 expired。定时任务调用。"""
    now = now if now is not None else time.time()
    conn = _conn()
    try:
        n = conn.execute(
            "UPDATE approvals SET state=?, detail=? WHERE state=? AND expires_at < ?",
            (EXPIRED, "超过有效期未处理", PENDING, now)).rowcount
    finally:
        conn.close()
    return n


def list_items(state: str = "", limit: int = 50, sid: Any = "") -> list[Approval]:
    where, args = [], []
    if state:
        where.append("state=?"); args.append(state)
    if sid:
        where.append("sid=?"); args.append(str(sid))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM approvals{clause} ORDER BY created_at DESC LIMIT ?",
            args + [int(limit)]).fetchall()
    finally:
        conn.close()
    return [_row_to_approval(r) for r in rows]


def summary() -> dict[str, int]:
    conn = _conn()
    try:
        rows = conn.execute("SELECT state, COUNT(*) c FROM approvals GROUP BY state").fetchall()
    finally:
        conn.close()
    return {r["state"]: int(r["c"]) for r in rows}


def render(items: list[Approval]) -> str:
    if not items:
        return "（无审批项）\n"
    out = []
    for a in items:
        age = (time.time() - a.created_at) / 3600.0
        out.append(f"  {a.id}  {a.describe()}  {age:.1f}小时前"
                   + (f"  审计 {a.audit_id}" if a.audit_id else ""))
    return "\n".join(out) + "\n"
