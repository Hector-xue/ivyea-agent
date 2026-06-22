"""Persistent local task runner for long-running agent work."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import config, security

TASK_DIR = config.IVYEA_DIR / "tasks"
TASK_STATUSES = {"pending", "in_progress", "blocked", "completed", "cancelled"}
STEP_STATUSES = {"pending", "in_progress", "blocked", "completed", "skipped"}


def _now() -> float:
    return time.time()


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "-", text.strip()).strip("-").lower()
    return s[:40] or "task"


def _task_path(task_id: str) -> Path:
    return TASK_DIR / f"{task_id}.json"


def _ensure() -> None:
    config.ensure_dirs()
    TASK_DIR.mkdir(parents=True, exist_ok=True)


def _save(task: dict[str, Any]) -> dict[str, Any]:
    _ensure()
    task["updated_at"] = _now()
    _task_path(task["id"]).write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return task


def create(title: str, steps: list[str] | None = None, notes: str = "", workspace: str = "") -> dict[str, Any]:
    title = title.strip()
    if not title:
        raise ValueError("title 不能为空")
    ts = time.strftime("%Y%m%d%H%M%S", time.localtime())
    task_id = f"{ts}-{_slug(title)}"
    task = {
        "id": task_id,
        "title": title,
        "status": "pending",
        "workspace": str(Path(workspace or ".").expanduser().resolve()) if workspace else "",
        "created_at": _now(),
        "updated_at": _now(),
        "notes": security.redact_text(notes or ""),
        "steps": [
            {"index": i + 1, "title": security.redact_text(s), "status": "pending", "notes": ""}
            for i, s in enumerate(steps or [])
            if str(s).strip()
        ],
        "events": [],
    }
    add_event(task, "created", "任务创建")
    return _save(task)


def load(task_id: str) -> dict[str, Any]:
    path = _task_path(task_id)
    if not path.exists():
        raise FileNotFoundError(f"任务不存在：{task_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_tasks(limit: int = 20, status: str = "") -> list[dict[str, Any]]:
    _ensure()
    rows = []
    for path in TASK_DIR.glob("*.json"):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if status and task.get("status") != status:
            continue
        rows.append(task)
    rows.sort(key=lambda t: float(t.get("updated_at") or 0), reverse=True)
    return rows[:limit]


def add_event(task: dict[str, Any], kind: str, text: str) -> dict[str, Any]:
    task.setdefault("events", []).append({
        "ts": _now(),
        "kind": kind,
        "text": security.redact_text(text or "")[:1200],
    })
    return task


def set_status(task_id: str, status: str, note: str = "") -> dict[str, Any]:
    if status not in TASK_STATUSES:
        raise ValueError(f"未知任务状态：{status}")
    task = load(task_id)
    task["status"] = status
    add_event(task, "status", f"{status}: {note}".strip(": "))
    return _save(task)


def update_step(task_id: str, index: int, status: str, note: str = "") -> dict[str, Any]:
    if status not in STEP_STATUSES:
        raise ValueError(f"未知步骤状态：{status}")
    task = load(task_id)
    steps = task.get("steps") or []
    if index < 1 or index > len(steps):
        raise IndexError(f"步骤不存在：{index}")
    steps[index - 1]["status"] = status
    if note:
        steps[index - 1]["notes"] = security.redact_text(note)
    if status == "in_progress" and task.get("status") == "pending":
        task["status"] = "in_progress"
    if steps and all(s.get("status") in {"completed", "skipped"} for s in steps):
        task["status"] = "completed"
    elif status == "blocked":
        task["status"] = "blocked"
    add_event(task, "step", f"#{index} {status}: {note}".strip(": "))
    return _save(task)


def start_next(task_id: str, note: str = "") -> dict[str, Any]:
    task = load(task_id)
    for step in task.get("steps") or []:
        if step.get("status") == "pending":
            return update_step(task_id, int(step["index"]), "in_progress", note)
    if task.get("steps"):
        task["status"] = "completed"
        add_event(task, "status", "completed: 没有待处理步骤")
    else:
        task["status"] = "in_progress"
        add_event(task, "status", "in_progress: 无步骤任务已开始")
    return _save(task)


def append_log(task_id: str, text: str, kind: str = "log") -> dict[str, Any]:
    task = load(task_id)
    add_event(task, kind, text)
    return _save(task)


def record_interruption(task_id: str, reason: str, note: str = "") -> dict[str, Any]:
    task = load(task_id)
    text = f"{reason}: {note}".strip(": ")
    active = next_step(task)
    if active and active.get("status") in {"pending", "in_progress"}:
        active["status"] = "blocked"
        if note:
            active["notes"] = security.redact_text(note)
    if task.get("status") not in {"completed", "cancelled"}:
        task["status"] = "blocked"
    add_event(task, "interrupted", text)
    return _save(task)


def progress(task: dict[str, Any]) -> dict[str, int]:
    steps = task.get("steps") or []
    total = len(steps)
    done = sum(1 for s in steps if s.get("status") in {"completed", "skipped"})
    blocked = sum(1 for s in steps if s.get("status") == "blocked")
    active = sum(1 for s in steps if s.get("status") == "in_progress")
    return {"total": total, "done": done, "blocked": blocked, "active": active}


def next_step(task: dict[str, Any]) -> dict[str, Any] | None:
    for status in ("in_progress", "pending", "blocked"):
        for step in task.get("steps") or []:
            if step.get("status") == status:
                return step
    return None


def render_list(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "（暂无任务）"
    lines = ["Ivyea Tasks", ""]
    for task in tasks:
        p = progress(task)
        ts = time.strftime("%m-%d %H:%M", time.localtime(float(task.get("updated_at") or 0)))
        lines.append(
            f"- {task['id']} [{task.get('status')}] {p['done']}/{p['total']} · {ts} · {task.get('title')}"
        )
    return "\n".join(lines)


def render(task: dict[str, Any]) -> str:
    p = progress(task)
    lines = [
        "Ivyea Task",
        "",
        f"- id: {task.get('id')}",
        f"- title: {task.get('title')}",
        f"- status: {task.get('status')}",
        f"- progress: {p['done']}/{p['total']} done, {p['active']} active, {p['blocked']} blocked",
    ]
    if task.get("workspace"):
        lines.append(f"- workspace: {task.get('workspace')}")
    if task.get("notes"):
        lines.append(f"- notes: {task.get('notes')}")
    if task.get("steps"):
        lines.append("")
        lines.append("Steps:")
        for step in task["steps"]:
            note = f" · {step.get('notes')}" if step.get("notes") else ""
            lines.append(f"- #{step['index']} [{step.get('status')}] {step.get('title')}{note}")
    if task.get("events"):
        lines.append("")
        lines.append("Recent events:")
        for ev in task["events"][-8:]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(float(ev.get("ts") or 0)))
            lines.append(f"- {ts} {ev.get('kind')}: {ev.get('text')}")
    return "\n".join(lines)


def render_resume(task: dict[str, Any]) -> str:
    step = next_step(task)
    lines = [
        "Ivyea Task Resume",
        "",
        f"- id: {task.get('id')}",
        f"- title: {task.get('title')}",
        f"- status: {task.get('status')}",
    ]
    if step:
        lines.append(f"- next: #{step.get('index')} [{step.get('status')}] {step.get('title')}")
        if step.get("notes"):
            lines.append(f"- note: {step.get('notes')}")
    else:
        lines.append("- next: 无待处理步骤")
    return "\n".join(lines)
