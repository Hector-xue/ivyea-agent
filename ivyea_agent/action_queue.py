"""Local action queue for reviewable Amazon operations."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict
from typing import Any

from . import config
from .actions import Action

QUEUE_FILE = config.IVYEA_DIR / "action_queue.jsonl"


def _fingerprint(payload: dict[str, Any], source: str = "") -> str:
    basis = {
        "source": source,
        "kind": payload.get("kind"),
        "asin": payload.get("asin"),
        "search_term": payload.get("search_term"),
        "new_bid": payload.get("new_bid"),
        "negate_match": payload.get("negate_match"),
    }
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _read() -> list[dict[str, Any]]:
    if not QUEUE_FILE.exists():
        return []
    rows = []
    for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _write(rows: list[dict[str, Any]]) -> None:
    config.ensure_dirs()
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    QUEUE_FILE.write_text((text + "\n") if text else "", encoding="utf-8")


def enqueue_actions(actions: list[Action], *, source: str = "", origin: str = "manual") -> list[dict[str, Any]]:
    rows = _read()
    existing = {r.get("fingerprint") for r in rows if r.get("status") in ("pending", "approved")}
    added = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for a in actions:
        payload = asdict(a)
        fp = _fingerprint(payload, source=source)
        if fp in existing:
            continue
        item = {
            "id": uuid.uuid4().hex[:12],
            "ts": now,
            "updated": now,
            "status": "pending",
            "origin": origin,
            "source": source,
            "fingerprint": fp,
            "kind": a.kind,
            "summary": a.summary(),
            "blocked": a.blocked,
            "block_reason": a.block_reason,
            "payload": payload,
        }
        rows.append(item)
        added.append(item)
        existing.add(fp)
    _write(rows)
    return added


def list_items(status: str = "", limit: int = 50) -> list[dict[str, Any]]:
    rows = _read()
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows[-limit:][::-1]


def get(item_id: str) -> dict[str, Any] | None:
    return next((r for r in _read() if r.get("id") == item_id), None)


def to_action(item: dict[str, Any]) -> Action:
    payload = dict(item.get("payload") or {})
    valid = {f.name for f in Action.__dataclass_fields__.values()}
    payload = {k: v for k, v in payload.items() if k in valid}
    return Action(**payload)


def set_status(item_id: str, status: str) -> bool:
    if status not in ("pending", "approved", "denied", "done"):
        raise ValueError("status must be pending/approved/denied/done")
    rows = _read()
    changed = False
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        if r.get("id") == item_id:
            r["status"] = status
            r["updated"] = now
            changed = True
            break
    if changed:
        _write(rows)
    return changed


def set_many_status(status: str, *, from_status: str = "pending", limit: int = 50,
                    include_blocked: bool = False) -> int:
    if status not in ("pending", "approved", "denied", "done"):
        raise ValueError("status must be pending/approved/denied/done")
    rows = _read()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    changed = 0
    for r in rows:
        if from_status and r.get("status") != from_status:
            continue
        if r.get("blocked") and not include_blocked:
            continue
        r["status"] = status
        r["updated"] = now
        changed += 1
        if changed >= limit:
            break
    if changed:
        _write(rows)
    return changed


def mark_done(item_id: str, detail: str = "") -> bool:
    rows = _read()
    changed = False
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        if r.get("id") == item_id:
            r["status"] = "done"
            r["updated"] = now
            if detail:
                r["result"] = detail
            changed = True
            break
    if changed:
        _write(rows)
    return changed


def clear(status: str = "") -> int:
    rows = _read()
    if not status:
        n = len(rows)
        _write([])
        return n
    keep = [r for r in rows if r.get("status") != status]
    n = len(rows) - len(keep)
    _write(keep)
    return n


def render(items: list[dict[str, Any]]) -> str:
    if not items:
        return "（动作队列为空）"
    lines = []
    for it in items:
        flag = "BLOCKED" if it.get("blocked") else it.get("status", "?").upper()
        lines.append(f"{it.get('id')}  {flag:<8}  {it.get('kind','')}  {it.get('summary','')}")
        if it.get("blocked") and it.get("block_reason"):
            lines.append(f"    拦截原因: {it['block_reason']}")
    return "\n".join(lines)


def summary() -> dict[str, int]:
    out = {"pending": 0, "approved": 0, "denied": 0, "done": 0, "blocked": 0}
    for item in _read():
        status = str(item.get("status") or "pending")
        out[status] = out.get(status, 0) + 1
        if item.get("blocked"):
            out["blocked"] += 1
    return out


def render_report(items: list[dict[str, Any]], title: str = "Ivyea 动作队列复核报告") -> str:
    lines = [
        f"# {title}",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 动作数：{len(items)}",
        "",
        "| ID | 状态 | 类型 | 来源 | 摘要 | 拦截原因 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        lines.append("| " + " | ".join([
            str(item.get("id", "")),
            "BLOCKED" if item.get("blocked") else str(item.get("status", "")),
            str(item.get("kind", "")),
            str(item.get("source", "")),
            str(item.get("summary", "")).replace("|", "\\|"),
            str(item.get("block_reason", "")).replace("|", "\\|"),
        ]) + " |")
    return "\n".join(lines) + "\n"
