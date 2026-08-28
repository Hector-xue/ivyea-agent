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
        "resume": {},
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
    if status == "in_progress" and task.get("status") in {"pending", "blocked"}:
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


def record_interruption(
    task_id: str,
    reason: str,
    note: str = "",
    *,
    resume_prompt: str = "",
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = load(task_id)
    text = f"{reason}: {note}".strip(": ")
    active = next_step(task)
    if active and active.get("status") in {"pending", "in_progress"}:
        active["status"] = "blocked"
        if note:
            active["notes"] = security.redact_text(note)
    if task.get("status") not in {"completed", "cancelled"}:
        task["status"] = "blocked"
    task["resume"] = resume_state(task, reason=reason, note=note, prompt=resume_prompt, state=state or {})
    add_event(task, "interrupted", text)
    return _save(task)


def resume_state(
    task: dict[str, Any],
    *,
    reason: str = "",
    note: str = "",
    prompt: str = "",
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured resume payload for CLI, API and IvyeaOps."""
    step = next_step(task)
    safe_state = _safe_state(state or {})
    resume_prompt = security.redact_text(prompt or _default_resume_prompt(task, step, reason, note, safe_state))
    data = {
        "reason": security.redact_text(reason or str((task.get("resume") or {}).get("reason") or "")),
        "note": security.redact_text(note or str((task.get("resume") or {}).get("note") or "")),
        "prompt": resume_prompt[:4000],
        "state": safe_state,
        "updated_at": _now(),
        "next_step": dict(step) if step else None,
    }
    return data


def resume_payload(task_id: str) -> dict[str, Any]:
    task = load(task_id)
    resume = task.get("resume") if isinstance(task.get("resume"), dict) else {}
    if resume and resume.get("prompt"):
        return {"ok": True, "task": task, "resume": resume}
    return {"ok": True, "task": task, "resume": resume_state(task)}


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
    resume = task.get("resume") if isinstance(task.get("resume"), dict) else {}
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
    prompt = str(resume.get("prompt") or resume_state(task).get("prompt") or "").strip()
    if prompt:
        lines.extend(["", "Resume prompt:", prompt])
    return "\n".join(lines)


def _safe_state(state: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in state.items():
        if isinstance(value, (int, float, bool)) or value is None:
            out[str(key)] = value
        elif isinstance(value, str):
            out[str(key)] = security.redact_text(value)[:1000]
        elif isinstance(value, list):
            out[str(key)] = [
                security.redact_text(str(item))[:500]
                for item in value[:20]
            ]
        elif isinstance(value, dict):
            out[str(key)] = _safe_state(value)
        else:
            out[str(key)] = security.redact_text(str(value))[:500]
    return out


def _default_resume_prompt(
    task: dict[str, Any],
    step: dict[str, Any] | None,
    reason: str,
    note: str,
    state: dict[str, Any],
) -> str:
    lines = [
        f"继续 Ivyea 长任务：{task.get('title')}",
        f"任务 ID：{task.get('id')}",
        f"当前状态：{task.get('status')}",
    ]
    if reason:
        lines.append(f"暂停原因：{reason}")
    if state:
        tool_calls = state.get("tool_calls")
        max_steps = state.get("max_steps")
        if tool_calls is not None or max_steps is not None:
            lines.append(f"上一轮工具调用：{tool_calls or 0}/{max_steps or '?'}")
        if state.get("session_id"):
            lines.append(f"会话：{state.get('session_id')}")
        if state.get("turn_id"):
            lines.append(f"轮次：{state.get('turn_id')}")
    if step:
        lines.append(f"下一步：#{step.get('index')} [{step.get('status')}] {step.get('title')}")
        if step.get("notes") or note:
            lines.append(f"步骤备注：{step.get('notes') or note}")
    else:
        lines.append("下一步：检查任务是否已完成，若未完成则列出剩余工作。")
    lines.extend([
        "继续要求：",
        "1. 先读取当前任务状态和最近事件，确认已完成与未完成事项。",
        "2. 从上面“下一步”继续，不要重复上一轮已经成功的工具调用。",
        "3. 若仍然接近工具上限，先保存进度并给出下一段续跑提示。",
    ])
    return "\n".join(lines)


def _rollup_status(task: dict[str, Any]) -> None:
    """按步骤状态推算任务状态（`sync_plan_steps` 专用）。

    不复用 `update_step` 里那几行：它是"这一步刚改成 X"的增量口径，而这里拿到的是
    整张表的快照，两者判定顺序本来就不同（增量口径下"本次改成 blocked"才置 blocked，
    快照口径下要问"还有没有别的步骤在跑"）。硬合成一个函数只会让两边都别扭。

    `cancelled` 不参与推算 —— 人工取消的任务不该被一次步骤同步复活。
    """
    steps = task.get("steps") or []
    if not steps or task.get("status") == "cancelled":
        return
    if all(s.get("status") in {"completed", "skipped"} for s in steps):
        task["status"] = "completed"
    elif any(s.get("status") == "in_progress" for s in steps):
        # 进行中优先于阻塞：第 1 步卡住、模型已经在跑第 3 步，任务就是在推进，
        # 报成 blocked 会让任务台上一堆"卡住"的任务其实都在跑。
        task["status"] = "in_progress"
    elif any(s.get("status") == "blocked" for s in steps):
        task["status"] = "blocked"
    elif task.get("status") == "completed":
        task["status"] = "in_progress"   # 计划又长出新步骤：已完成的任务重新打开


def sync_plan_steps(task_id: str, steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """用计划台账的步骤覆盖任务步骤表。**只由 `plan_store` 调**。

    为什么要有这一条：`task_runner` 的步骤表有自己的消费方 —— `ivyea task show`、
    `/v1/task/{id}`，以及**续跑提示**（`task_continue` 按 `next_step` 生成"下一步该干
    什么"）。在这之前那份表只有模型显式调 `task_step` 才会动，而模型实际维护的是
    `todo_write`：一份计划两处记，结果就是续跑照着一份过期的步骤表指路。

    为什么是覆盖而不是合并：任务文件里的 `steps` 从此是计划的**投影**，不是第二份
    真相。ADR-0026 留下的"两套步骤表示"就是靠这一条消除的 —— 合并只会把分歧原样留下。

    不写空：`steps` 为空一律原样返回。计划在换一轮查询时会被 `plan_store.reset` 清空，
    那不代表用户在任务台里排的步骤该被抹掉。
    """
    if not task_id or not steps:
        return None
    try:
        task = load(task_id)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None                      # 任务文件没了/坏了：投影是增益，不能反过来炸掉计划
    if task.get("status") == "cancelled":
        return task
    new_steps: list[dict[str, Any]] = []
    for idx, step in enumerate(steps, 1):
        status = str(step.get("status") or "pending")
        if status not in STEP_STATUSES:
            status = "pending"
        note = str(step.get("notes") or "")
        evidence = [str(e) for e in (step.get("evidence") or []) if str(e).strip()]
        if not note and evidence:
            note = evidence[-1]          # 续跑要的是"上一步拿到了什么"，证据比空备注有用
        new_steps.append({
            "index": idx,
            "title": security.redact_text(str(step.get("content") or step.get("title") or "")),
            "status": status,
            "notes": security.redact_text(note),
        })
    previous = task.get("steps") or []
    if new_steps == previous:
        return task                      # 没变就不写：events 不该被每次 todo_write 刷成流水账
    task["steps"] = new_steps
    _rollup_status(task)
    # 只有**步骤或状态**变了才记事件。挂证据（`attach_evidence`）也会走到这里，那只改备注 ——
    # 给它记一条和上一条一字不差的"计划同步：2/4 完成"，等于把真事件挤出最近 8 条。
    def _shape(steps):
        return [(s.get("title"), s.get("status")) for s in steps]
    if _shape(new_steps) != _shape(previous):
        done = sum(1 for s in new_steps if s.get("status") in {"completed", "skipped"})
        running = next((s for s in new_steps if s.get("status") == "in_progress"), None)
        text = f"计划同步：{done}/{len(new_steps)} 完成"
        if running:
            text += f"，进行中 #{running['index']} {running['title']}"
        add_event(task, "plan", text)
    return _save(task)
