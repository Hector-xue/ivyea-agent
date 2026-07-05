"""Deterministic progress-reporting state for multi-step agent work.

The model supplies human-readable summaries, while this module anchors them to
the actual todo state and observed tool results.  That prevents a polished
final answer from silently hiding pending or blocked work.
"""
from __future__ import annotations

from typing import Any

from . import security


TERMINAL_TODO_STATUSES = {"completed", "blocked", "skipped"}
TODO_STATUSES = {"pending", "in_progress", *TERMINAL_TODO_STATUSES}
META_TOOLS = {
    "progress_update", "todo_write", "self_critique",
    "task_read", "task_step", "task_log", "task_resume",
}


def _text(value: Any, limit: int = 500) -> str:
    return " ".join(security.redact_text(str(value or "")).split())[:limit]


def _items(value: Any, *, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:limit]:
        clean = _text(item)
        if clean and clean not in out:
            out.append(clean)
    return out


def _dedupe(*groups: list[str], limit: int = 30) -> list[str]:
    out: list[str] = []
    for group in groups:
        for item in group:
            clean = _text(item)
            if clean and clean not in out:
                out.append(clean)
            if len(out) >= limit:
                return out
    return out


def reset(ctx: Any, *, clear_todos: bool = True) -> None:
    """Reset one task's reporting state without disturbing session scope."""
    ctx.progress_started = False
    ctx.progress_start = {}
    ctx.progress_active_phase = 0
    ctx.progress_started_phases = set()
    ctx.progress_phase_reports = {}
    ctx.progress_final = {}
    ctx.progress_tool_evidence = []
    ctx.progress_phase_tool_evidence = {}
    ctx.progress_attention = []
    ctx.progress_last_event = {}
    if clear_todos:
        ctx.todos = []


def prepare_context(ctx: Any, query: str, *, required: bool,
                    execution_expected: bool = False, continuation: bool = False) -> None:
    """Start a new reporting task or preserve an unfinished continuation."""
    clean = _text(query, 1000)
    previous = _text(getattr(ctx, "progress_query", ""), 1000)
    disabled = bool(getattr(ctx, "progress_reporting_disabled", False))
    if disabled:
        ctx.progress_required = False
        ctx.progress_execution_expected = False
        ctx.progress_query = clean
        return
    if clean == previous:
        ctx.progress_required = bool(getattr(ctx, "progress_required", False) or required)
        ctx.progress_execution_expected = bool(
            getattr(ctx, "progress_execution_expected", False) or execution_expected)
        return
    unfinished_continuation = bool(
        continuation
        and getattr(ctx, "progress_required", False)
        and not getattr(ctx, "progress_final", {})
    )
    if not unfinished_continuation:
        reset(ctx, clear_todos=bool(previous))
        ctx.progress_required = bool(required)
        ctx.progress_execution_expected = bool(execution_expected)
    else:
        ctx.progress_required = bool(getattr(ctx, "progress_required", False) or required)
        ctx.progress_execution_expected = bool(
            getattr(ctx, "progress_execution_expected", False) or execution_expected)
    ctx.progress_query = clean


def is_substantive_tool(name: str) -> bool:
    return bool(name and name not in META_TOOLS)


def _todo(ctx: Any, index: int) -> dict[str, Any] | None:
    todos = list(getattr(ctx, "todos", []) or [])
    if index < 1 or index > len(todos):
        return None
    item = todos[index - 1]
    return item if isinstance(item, dict) else None


def _current_index(ctx: Any) -> int:
    for index, item in enumerate(getattr(ctx, "todos", []) or [], 1):
        if isinstance(item, dict) and item.get("status") == "in_progress":
            return index
    return 0


def _phase_index(args: dict[str, Any], ctx: Any) -> int:
    raw = args.get("phase_index")
    if raw in (None, ""):
        return _current_index(ctx)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _failure(message: str) -> dict[str, Any]:
    return {"ok": False, "text": "⚠ " + message, "event": {}}


def _success(event: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "text": render_plain(event), "event": event}


def apply_update(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Validate and record a start/phase/final report."""
    kind = _text(args.get("kind"), 40).lower()
    if kind not in {"start", "phase_start", "phase_end", "final"}:
        return _failure("progress_update.kind 必须是 start、phase_start、phase_end 或 final。")
    if kind != "start" and not getattr(ctx, "progress_started", False):
        return _failure("尚未开始任务汇报；先调用 progress_update(kind='start')。")

    if kind == "start":
        todos = list(getattr(ctx, "todos", []) or [])
        if not todos:
            return _failure("开始执行前先用 todo_write 建立多步计划。")
        objective = _text(args.get("summary") or args.get("objective"))
        criteria = _items(args.get("success_criteria"))
        index = _phase_index(args, ctx)
        item = _todo(ctx, index)
        current = [i for i, todo in enumerate(todos, 1)
                   if isinstance(todo, dict) and todo.get("status") == "in_progress"]
        if not objective:
            return _failure("开始汇报缺少 summary（本次目标）。")
        if not criteria:
            return _failure("开始汇报缺少 success_criteria（完成判定标准）。")
        if len(current) != 1 or not item or item.get("status") != "in_progress" or current[0] != index:
            return _failure("开始汇报必须指向 Todo 中唯一的 in_progress 阶段。")
        scope = _items(args.get("scope"))
        target = _text(getattr(ctx, "target_project", ""))
        root = _text(getattr(ctx, "target_root", "") or getattr(ctx, "workspace", ""))
        automatic_scope = ([f"项目：{target}"] if target else []) + ([f"工作目录：{root}"] if root else [])
        scope = _dedupe(automatic_scope, scope) or ["当前会话任务范围"]
        event = {
            "kind": "start",
            "objective": objective,
            "scope": scope,
            "plan": [_text(todo.get("content")) for todo in todos if isinstance(todo, dict)],
            "success_criteria": criteria,
            "phase_index": index,
            "phase": _text(item.get("content")),
            "summary": _text(args.get("next") or item.get("content")),
        }
        ctx.progress_required = True
        ctx.progress_final = {}
        ctx.progress_started = True
        ctx.progress_start = event
        ctx.progress_active_phase = index
        started = set(getattr(ctx, "progress_started_phases", set()) or set())
        started.add(index)
        ctx.progress_started_phases = started
        ctx.progress_last_event = event
        return _success(event)

    if kind == "phase_start":
        if int(getattr(ctx, "progress_active_phase", 0) or 0):
            return _failure("上一阶段尚未 phase_end，不能开始新阶段。")
        index = _phase_index(args, ctx)
        item = _todo(ctx, index)
        summary = _text(args.get("summary"))
        if not item or item.get("status") != "in_progress":
            return _failure("phase_start 必须指向 Todo 中当前的 in_progress 阶段。")
        if not summary:
            return _failure("phase_start 缺少 summary（本阶段准备做什么）。")
        event = {"kind": "phase_start", "phase_index": index,
                 "phase": _text(item.get("content")), "summary": summary}
        ctx.progress_active_phase = index
        ctx.progress_final = {}
        started = set(getattr(ctx, "progress_started_phases", set()) or set())
        started.add(index)
        ctx.progress_started_phases = started
        ctx.progress_last_event = event
        return _success(event)

    if kind == "phase_end":
        index = _phase_index(args, ctx) or int(getattr(ctx, "progress_active_phase", 0) or 0)
        active = int(getattr(ctx, "progress_active_phase", 0) or 0)
        item = _todo(ctx, index)
        status = _text(args.get("status"), 20).lower()
        summary = _text(args.get("summary"))
        completed = _items(args.get("completed"))
        incomplete = _items(args.get("incomplete"))
        evidence = _items(args.get("evidence"))
        attention = _items(args.get("attention"))
        observed = list((getattr(ctx, "progress_phase_tool_evidence", {}) or {}).get(index, []) or [])
        if not active or index != active or not item:
            return _failure("phase_end 必须结束当前正在汇报的阶段。")
        if status not in {"completed", "partial", "blocked", "skipped"}:
            return _failure("phase_end.status 必须是 completed、partial、blocked 或 skipped。")
        if not summary:
            return _failure("phase_end 缺少 summary（本阶段做了什么）。")
        if status in {"completed", "partial"} and not evidence:
            return _failure("完成或部分完成的阶段必须提供 evidence。")
        if status in {"completed", "partial"} and not observed:
            return _failure("完成或部分完成的阶段还没有真实工具结果，不能只凭文字声称完成。")
        if status in {"partial", "blocked"} and not incomplete:
            return _failure("部分完成或阻塞的阶段必须说明 incomplete。")
        if status == "blocked" and not attention:
            return _failure("阻塞阶段必须在 attention 中说明原因或风险。")
        evidence = _dedupe(evidence, observed, limit=8)
        event = {
            "kind": "phase_end", "phase_index": index,
            "phase": _text(item.get("content")), "status": status,
            "summary": summary, "completed": completed,
            "incomplete": incomplete, "evidence": evidence,
            "attention": attention, "next": _text(args.get("next")),
        }
        reports = dict(getattr(ctx, "progress_phase_reports", {}) or {})
        reports[index] = event
        ctx.progress_phase_reports = reports
        ctx.progress_active_phase = 0
        ctx.progress_final = {}
        ctx.progress_last_event = event
        return _success(event)

    # final
    if int(getattr(ctx, "progress_active_phase", 0) or 0):
        return _failure("当前阶段尚未 phase_end，不能生成最终汇总。")
    if any(item.get("status") == "in_progress" for item in getattr(ctx, "todos", []) or []
           if isinstance(item, dict)):
        return _failure("Todo 仍有 in_progress；先结束阶段并更新状态。")
    summary = _text(args.get("summary"))
    if not summary:
        return _failure("最终汇总缺少 summary（整体结果）。")
    todos = list(getattr(ctx, "todos", []) or [])
    actual_completed = [_text(item.get("content")) for item in todos
                        if isinstance(item, dict) and item.get("status") == "completed"]
    actual_incomplete = [f"{_text(item.get('content'))}（{item.get('status', 'pending')}）"
                         for item in todos if isinstance(item, dict)
                         and item.get("status") != "completed"]
    reports = list((getattr(ctx, "progress_phase_reports", {}) or {}).values())
    report_evidence = [e for report in reports for e in _items(report.get("evidence"))]
    report_attention = [e for report in reports for e in _items(report.get("attention"))]
    completed = _dedupe(actual_completed, _items(args.get("completed")), limit=20) or ["无"]
    incomplete = _dedupe(actual_incomplete, _items(args.get("incomplete")), limit=20) or ["无"]
    evidence = _dedupe(_items(args.get("evidence")), report_evidence,
                       list(getattr(ctx, "progress_tool_evidence", []) or []), limit=12) or ["无可用验证证据"]
    attention = _dedupe(_items(args.get("attention")), report_attention,
                        list(getattr(ctx, "progress_attention", []) or []), limit=10)
    if actual_incomplete:
        attention = _dedupe(attention, ["仍有未完成步骤，不能视为全部完成。"], limit=10)
    elif not attention:
        attention = ["无已知特别事项"]
    event = {
        "kind": "final", "summary": summary,
        "completed": completed, "incomplete": incomplete,
        "evidence": evidence, "attention": attention,
    }
    ctx.progress_final = event
    ctx.progress_last_event = event
    return _success(event)


def validate_todo_update(ctx: Any, todos: list[dict[str, Any]]) -> str:
    """Require a matching phase report before a Todo becomes terminal."""
    old = list(getattr(ctx, "todos", []) or [])
    if not old or not getattr(ctx, "progress_required", False):
        return ""
    reports = dict(getattr(ctx, "progress_phase_reports", {}) or {})
    active = int(getattr(ctx, "progress_active_phase", 0) or 0)
    for index, item in enumerate(todos, 1):
        previous = old[index - 1] if index <= len(old) and isinstance(old[index - 1], dict) else {}
        old_status = previous.get("status", "pending")
        new_status = item.get("status", "pending")
        if index in reports and _text(previous.get("content")) != _text(item.get("content")):
            return f"第 {index} 阶段已有汇报，不能再改名；如需新增工作请追加 Todo。"
        if old_status not in TERMINAL_TODO_STATUSES and new_status in TERMINAL_TODO_STATUSES:
            report = reports.get(index)
            if not report:
                return f"第 {index} 阶段转为 {new_status} 前，必须先 progress_update(kind='phase_end')。"
            report_status = report.get("status")
            valid = (
                (new_status == "completed" and report_status == "completed")
                or (new_status == "blocked" and report_status in {"partial", "blocked"})
                or (new_status == "skipped" and report_status == "skipped")
            )
            if not valid:
                return f"第 {index} 阶段汇报状态 {report_status} 与 Todo 状态 {new_status} 不一致。"
    next_active = [i for i, item in enumerate(todos, 1) if item.get("status") == "in_progress"]
    if len(next_active) > 1:
        return "同一时间只能有一个 in_progress 阶段。"
    if active and next_active and next_active[0] != active:
        return "当前阶段尚未 phase_end，不能把另一阶段标为 in_progress。"
    return ""


def observe_tool_result(ctx: Any, name: str, result: Any) -> None:
    """Collect bounded, real tool evidence and invalidate stale final reports."""
    evidence_meta = name == "self_critique"
    if ((not is_substantive_tool(name) and not evidence_meta)
            or not getattr(ctx, "progress_required", False)):
        return
    if getattr(ctx, "progress_final", {}):
        ctx.progress_final = {}
    first = _text((getattr(result, "text", "") or "").splitlines()[0] if getattr(result, "text", "") else "")
    if not first:
        return
    if any(marker in first for marker in (
        "复杂/多步任务实际执行前", "请先 progress_update(kind='start')",
        "当前没有已汇报开始的阶段",
    )):
        return
    failed = (not bool(getattr(result, "ok", False))
              or first.startswith("⚠")
              or any(marker in first for marker in ("失败", "错误", "已拦截", "拒绝")))
    target = list(getattr(ctx, "progress_attention" if failed else "progress_tool_evidence", []) or [])
    entry = f"{name}: {first}"
    if entry not in target:
        target.append(entry)
    target = target[-20:]
    if failed:
        ctx.progress_attention = target
    else:
        ctx.progress_tool_evidence = target
        active = int(getattr(ctx, "progress_active_phase", 0) or 0)
        if active:
            per_phase = dict(getattr(ctx, "progress_phase_tool_evidence", {}) or {})
            rows = list(per_phase.get(active, []) or [])
            if entry not in rows:
                rows.append(entry)
            per_phase[active] = rows[-20:]
            ctx.progress_phase_tool_evidence = per_phase


def completion_feedback(ctx: Any) -> str | None:
    """Return a corrective prompt when a reported task tries to finish early."""
    if (getattr(ctx, "progress_reporting_disabled", False)
            or not getattr(ctx, "progress_required", False)):
        return None
    if getattr(ctx, "plan_mode", False) and not getattr(ctx, "progress_started", False):
        return None
    has_workflow = bool(getattr(ctx, "todos", []) or getattr(ctx, "progress_started", False))
    if not has_workflow:
        if getattr(ctx, "progress_execution_expected", False):
            return ("[汇报门禁] 用户要求实际执行复杂任务，但尚未建立执行流程。"
                    "先 todo_write 列出阶段，再 progress_update(kind='start') 后开始工具执行。")
        return None
    if not getattr(ctx, "progress_started", False):
        return ("[汇报门禁] 已建立多步计划但尚未做开始汇报。先调用 progress_update(kind='start')，"
                "说明目标、范围、阶段和完成标准，再继续执行。")
    active = int(getattr(ctx, "progress_active_phase", 0) or 0)
    if active:
        return (f"[汇报门禁] 第 {active} 阶段尚未结束。先调用 progress_update(kind='phase_end')，"
                "汇报完成/未完成、证据和注意事项，再更新 Todo。")
    if not getattr(ctx, "progress_final", {}):
        return ("[汇报门禁] 执行结束前必须调用 progress_update(kind='final')。"
                "最终汇总需明确已做到、未做到、验证证据和注意事项；系统会合并真实 Todo 与工具证据。")
    return None


def render_plain(event: dict[str, Any]) -> str:
    """Stable plain-text form returned to the model as the tool result."""
    kind = event.get("kind")
    if kind == "start":
        return (f"开始执行：{event.get('objective')}\n"
                f"当前阶段 {event.get('phase_index')}：{event.get('phase')}\n"
                f"完成标准：{'；'.join(event.get('success_criteria') or [])}")
    if kind == "phase_start":
        return f"阶段 {event.get('phase_index')} 开始：{event.get('phase')}\n计划：{event.get('summary')}"
    if kind == "phase_end":
        return (f"阶段 {event.get('phase_index')} 结束（{event.get('status')}）：{event.get('phase')}\n"
                f"总结：{event.get('summary')}")
    return (f"执行汇总：{event.get('summary')}\n"
            f"已做到：{'；'.join(event.get('completed') or [])}\n"
            f"未做到：{'；'.join(event.get('incomplete') or [])}\n"
            f"验证：{'；'.join(event.get('evidence') or [])}\n"
            f"注意：{'；'.join(event.get('attention') or [])}")


def public_state(ctx: Any) -> dict[str, Any]:
    return {
        "required": bool(getattr(ctx, "progress_required", False)),
        "execution_expected": bool(getattr(ctx, "progress_execution_expected", False)),
        "started": bool(getattr(ctx, "progress_started", False)),
        "start": dict(getattr(ctx, "progress_start", {}) or {}),
        "active_phase": int(getattr(ctx, "progress_active_phase", 0) or 0),
        "phases": list((getattr(ctx, "progress_phase_reports", {}) or {}).values()),
        "final": dict(getattr(ctx, "progress_final", {}) or {}),
    }
