"""计划台账：把多步任务的计划变成一件**看得见、改得动、丢不掉**的东西。

为什么要有这一层
----------------
在这之前，计划只活在 `ToolContext.todos` 这个内存列表里，而且**从来没有被注回模型的
上下文**。模型能知道自己的计划，靠的是它自己那几条 `todo_write` 工具调用还留在消息
历史里 —— 一旦 `/compact` 把历史压成摘要，计划就随之蒸发，模型接着干活时已经不知道
自己原本打算干几件事、干到第几件了。这与记忆此前踩的是同一个坑：**model-driven 的
东西必须改成 runtime-driven**，不能指望模型"记得自己说过什么"。

所以这里做三件事：

1. **落盘**：计划写 ``~/.ivyea/plans/<session>.json``，进程重启、会话续跑都还在。
2. **回注**：`render_note()` 把当前计划渲染成一段短文本，每轮由运行时注回上下文，
   压缩时也一并保留（见 `context.compact` 的 extra_note）—— 模型没有忘的机会。
3. **留痕**：计划被改写会记进 ``revisions``，用户看得见"这个计划中途改过几次、为什么"。

降级契约（重要）
----------------
**没有 session_id 就整个模块空转**：`sync_todos` 直接返回 None，`render_note` 返回空串。
只读子 agent、单元测试里的裸 ToolContext 都属于这一类，它们的行为必须与加这个模块之前
逐字相同。落盘失败（磁盘满、只读挂载）同样一律吞掉 —— 计划台账是增益，不能成为
"活干不下去"的新理由。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import config, security

#: 终态：进了这几个状态的步骤不再算"待办"。与 progress_reporting.TERMINAL_TODO_STATUSES 同义，
#: 这里独立写一份是为了避免 plan_store ← progress_reporting 的反向依赖（后者要 import 前者）。
TERMINAL_STATUSES = {"completed", "blocked", "skipped"}

#: 注回上下文的计划文本最多多少字符。计划是**每轮都要付费**的常驻上下文，
#: 不能因为模型列了 30 步就把 system 撑爆。
_NOTE_MAX_CHARS = 1200
#: 单个步骤标题截断长度。
_STEP_MAX_CHARS = 160
#: 保留多少条修订记录。只用来给人看"改过几次"，不需要无限攒。
_MAX_REVISIONS = 12
#: 每步最多挂几条证据。
_MAX_EVIDENCE = 6

PLAN_NOTE_MARKER = "[当前计划]"


def plans_dir() -> Path:
    return config.IVYEA_DIR / "plans"


def _now() -> float:
    return time.time()


def _safe_key(session_id: str) -> str:
    """session_id → 文件名。会话 id 来自上游（serve/飞书/CLI），不能直接当路径用。"""
    key = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(session_id or ""))
    return key[:120]


def path_for(session_id: str) -> Path | None:
    key = _safe_key(session_id)
    return (plans_dir() / f"{key}.json") if key else None


def load(session_id: str) -> dict[str, Any] | None:
    """读计划。不存在/损坏一律当作没有 —— 绝不让一个坏文件把对话卡死。"""
    path = path_for(session_id)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _project_to_task(plan: dict[str, Any]) -> None:
    """把计划步骤投影进任务文件（``~/.ivyea/tasks/<task_id>.json``）。

    单向依赖：``plan_store → task_runner``。反过来不成立（`task_runner` 不认识计划），
    所以不会成环，也不会在 save↔sync 之间来回写 —— `sync_plan_steps` 见到步骤没变就
    直接返回，不落盘。

    投影失败一律吞掉：任务文件是计划的**下游**，下游坏了不该让计划更新跟着失败。
    """
    task_id = str(plan.get("task_id") or "")
    if not task_id or not (plan.get("steps") or []):
        return
    try:
        from . import task_runner
        task_runner.sync_plan_steps(task_id, list(plan.get("steps") or []))
    except Exception:  # noqa: BLE001
        return


def save(plan: dict[str, Any]) -> dict[str, Any]:
    """原子落盘。写失败只吞掉不抛 —— 计划台账坏了不该连累这一轮任务。"""
    path = path_for(plan.get("session_id", ""))
    if path is None:
        return plan
    plan["updated_at"] = _now()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同目录临时文件 + rename：避免半截 JSON 被下一次 load 读到。
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".plan-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        return plan
    # 落盘成功才投影：任务文件是计划的投影，计划自己都没写成功就没什么可投的。
    _project_to_task(plan)
    return plan


def delete(session_id: str) -> bool:
    path = path_for(session_id)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _new_plan(session_id: str, task_id: str = "", query: str = "") -> dict[str, Any]:
    return {
        "id": _safe_key(session_id),
        "session_id": session_id,
        "task_id": task_id or "",
        "query": _clip(query, 400),
        "objective": "",
        "scope": [],
        "success_criteria": [],
        "steps": [],
        "approved_at": 0.0,
        "approved_by": "",
        "pending_approval": False,
        "replan_reason": "",
        "revisions": [],
        "created_at": _now(),
        "updated_at": _now(),
    }


def _clip(text: Any, limit: int) -> str:
    clean = " ".join(security.redact_text(str(text or "")).split())
    return clean[:limit]


def _steps_from_todos(todos: list, previous: list | None = None) -> list[dict[str, Any]]:
    """todo_write 的 [{content,status}] → 带序号/证据/时间戳的步骤。

    同一条步骤（按 content 匹配）在改写前后要保住它已经攒下的证据和开始时间，
    否则模型每调一次 todo_write 就把执行痕迹抹一遍。
    """
    old_by_content: dict[str, dict[str, Any]] = {}
    for item in previous or []:
        if isinstance(item, dict) and item.get("content"):
            old_by_content.setdefault(str(item["content"]), item)
    steps: list[dict[str, Any]] = []
    for idx, item in enumerate(todos or [], 1):
        if not isinstance(item, dict) or not item.get("content"):
            continue
        content = _clip(item["content"], _STEP_MAX_CHARS)
        prior = old_by_content.get(str(item["content"])) or {}
        status = str(item.get("status") or "pending")
        step = {
            "index": idx,
            "content": content,
            "status": status,
            "evidence": list(prior.get("evidence") or [])[:_MAX_EVIDENCE],
            "notes": str(prior.get("notes") or ""),
            "started_at": float(prior.get("started_at") or 0.0),
            "ended_at": float(prior.get("ended_at") or 0.0),
        }
        if status == "in_progress" and not step["started_at"]:
            step["started_at"] = _now()
        if status in TERMINAL_STATUSES and not step["ended_at"]:
            step["ended_at"] = _now()
        steps.append(step)
    return steps


def _digest(steps: list[dict[str, Any]]) -> list[str]:
    return [f"{s.get('content', '')}（{s.get('status', 'pending')}）" for s in steps]


def _is_revision(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool:
    """只有**步骤本身变了**才算修订；单纯推进状态不是修订。

    模型每完成一步都会重发一次完整 todo 列表，如果把那也记成修订，
    `revisions` 会变成一份和步骤状态一模一样的流水账，什么也说明不了。
    """
    return [s.get("content") for s in before] != [s.get("content") for s in after]


def sync_todos(session_id: str, todos: list, *, task_id: str = "",
               query: str = "", plan_mode: bool = False) -> dict[str, Any] | None:
    """`todo_write` 的落盘侧。没有 session_id 直接空转（返回 None）。"""
    if not _safe_key(session_id):
        return None
    plan = load(session_id) or _new_plan(session_id, task_id=task_id, query=query)
    if task_id:
        plan["task_id"] = task_id
    if query:
        plan["query"] = _clip(query, 400)
    before = list(plan.get("steps") or [])
    after = _steps_from_todos(todos, previous=before)
    if before and _is_revision(before, after):
        plan.setdefault("revisions", []).append({
            "at": _now(),
            "reason": _clip(plan.get("replan_reason") or "模型改写了计划", 200),
            "before": _digest(before),
        })
        plan["revisions"] = plan["revisions"][-_MAX_REVISIONS:]
        plan["replan_reason"] = ""     # 改写完成即视为已响应重规划要求
    plan["steps"] = after
    if plan_mode and after and not plan.get("approved_at"):
        plan["pending_approval"] = True
    return save(plan)


def record_start(session_id: str, *, objective: str = "", scope: list | None = None,
                 success_criteria: list | None = None) -> dict[str, Any] | None:
    """`progress_update(kind='start')` 的落盘侧：把目标/范围/完成标准记进计划。"""
    if not _safe_key(session_id):
        return None
    plan = load(session_id) or _new_plan(session_id)
    if objective:
        plan["objective"] = _clip(objective, 400)
    if scope:
        plan["scope"] = [_clip(s, 160) for s in list(scope)[:12] if str(s or "").strip()]
    if success_criteria:
        plan["success_criteria"] = [_clip(s, 200) for s in list(success_criteria)[:12]
                                    if str(s or "").strip()]
    return save(plan)


def attach_evidence(session_id: str, index: int, evidence: list | str) -> dict[str, Any] | None:
    """把某个阶段真正拿到的证据挂到对应步骤上（progress_update(phase_end) 调）。"""
    if not _safe_key(session_id):
        return None
    plan = load(session_id)
    if not plan:
        return None
    items = [evidence] if isinstance(evidence, str) else list(evidence or [])
    steps = plan.get("steps") or []
    if index < 1 or index > len(steps):
        return plan
    step = steps[index - 1]
    bucket = list(step.get("evidence") or [])
    for item in items:
        clean = _clip(item, 200)
        if clean and clean not in bucket:
            bucket.append(clean)
    step["evidence"] = bucket[-_MAX_EVIDENCE:]
    return save(plan)


def approve(session_id: str, by: str = "user") -> dict[str, Any] | None:
    """用户批准计划（`/approve`，或显式退出计划模式）。"""
    if not _safe_key(session_id):
        return None
    plan = load(session_id)
    if not plan:
        return None
    plan["approved_at"] = _now()
    plan["approved_by"] = by
    plan["pending_approval"] = False
    return save(plan)


def awaiting_approval(session_id: str) -> bool:
    plan = load(session_id)
    return bool(plan and plan.get("pending_approval") and not plan.get("approved_at"))


def mark_replan(session_id: str, reason: str) -> dict[str, Any] | None:
    """要求重新规划。理由会随下一次注回的计划文本一起摆到模型面前。"""
    if not _safe_key(session_id):
        return None
    plan = load(session_id)
    if not plan:
        return None
    plan["replan_reason"] = _clip(reason, 300)
    return save(plan)


def replan_reason(session_id: str) -> str:
    plan = load(session_id)
    return str((plan or {}).get("replan_reason") or "")


def clear_replan(session_id: str) -> None:
    plan = load(session_id)
    if plan and plan.get("replan_reason"):
        plan["replan_reason"] = ""
        save(plan)


def adopt_task(session_id: str, task_id: str) -> dict[str, Any] | None:
    """反向：任务已有步骤、计划还空着时，用任务步骤给计划**播种**。

    这条路专治"人在任务台排好步骤 → agent 接手"。在这之前模型只能从续跑提示里读到
    一句散文式的"下一步 #2 ..."，计划台账是空的，于是 `[当前计划]` 那段根本不注入 ——
    人排的步骤和运行时状态两张皮。

    **绝不覆盖模型自己的计划**：计划已经有步骤就原样返回。模型在干活途中改出来的计划
    比任务创建时那份新，这是播种不是同步。

    **只种台账，不碰 `ctx.todos`**：`todo_write` 那条路上挂着汇报门禁
    （`progress_reporting.validate_todo_update` 只在 `ctx.todos` 非空时生效），凭空把
    步骤塞进 `ctx.todos` 等于给模型无声地加了一道它没同意过的门禁。模型读到注回的
    `[当前计划]` 之后自己发一次 `todo_write`，走的是原本那条被校验过的路。
    """
    if not _safe_key(session_id) or not str(task_id or "").strip():
        return None
    plan = load(session_id)
    if plan and (plan.get("steps") or []):
        return plan
    try:
        from . import task_runner
        task = task_runner.load(task_id)
    except Exception:  # noqa: BLE001 —— 任务不存在/坏了：当作没有任务，行为与改造前一致
        return plan
    steps = [
        {"content": _clip(s.get("title") or "", _STEP_MAX_CHARS),
         "status": str(s.get("status") or "pending"),
         "notes": _clip(s.get("notes") or "", 200),
         "evidence": [], "started_at": 0.0, "ended_at": 0.0,
         "index": idx}
        for idx, s in enumerate(task.get("steps") or [], 1)
        if isinstance(s, dict) and str(s.get("title") or "").strip()
    ]
    if not steps:
        return plan
    plan = plan or _new_plan(session_id, task_id=task_id, query=_clip(task.get("title") or "", 400))
    plan["task_id"] = task_id
    if not plan.get("objective") and task.get("title"):
        plan["objective"] = _clip(task["title"], 400)
    plan["steps"] = steps
    return save(plan)


def reset(session_id: str, *, query: str = "", task_id: str = "") -> dict[str, Any] | None:
    """换任务了：清空步骤与批准状态，保留文件（`revisions` 也一并清 —— 那是上一个任务的账）。"""
    if not _safe_key(session_id):
        return None
    plan = _new_plan(session_id, task_id=task_id, query=query)
    return save(plan)


# ── 注回上下文 ───────────────────────────────────────────────────────────────
_STATUS_MARK = {
    "completed": "✓", "in_progress": "▶", "pending": "·",
    "blocked": "✗", "skipped": "⊘",
}


def render_note(session_id: str, *, plan: dict[str, Any] | None = None) -> str:
    """渲染注回模型上下文的 `[当前计划]` 段。没有计划/没有步骤时返回空串。

    刻意写得很短：这段每轮常驻，越长越贵。只给模型三样它真会忘的东西 ——
    目标、每步状态、以及"现在轮到哪一步"。
    """
    plan = plan if plan is not None else load(session_id)
    if not plan:
        return ""
    steps = plan.get("steps") or []
    if not steps:
        return ""
    # 措辞要点：这份状态是**本轮开始时**由运行时读出来的，轮内模型自己 todo_write
    # 推进过之后它就旧了。不写清楚这一点，模型会拿一份旧状态去覆盖自己刚做的推进。
    lines = [PLAN_NOTE_MARKER + " 本轮开始时的计划状态（运行时记录，跨上下文压缩不丢；"
             "本轮内你若已用 todo_write 更新过，以你最近一次更新为准）："]
    if plan.get("objective"):
        lines.append(f"目标：{plan['objective']}")
    criteria = plan.get("success_criteria") or []
    if criteria:
        lines.append("完成标准：" + "；".join(criteria[:5]))
    for step in steps:
        mark = _STATUS_MARK.get(str(step.get("status")), "·")
        line = f"{mark} {step.get('index')}. {step.get('content')}"
        ev = step.get("evidence") or []
        if ev:
            line += f"（证据：{ev[-1]}）"
        lines.append(line)
    done = sum(1 for s in steps if s.get("status") == "completed")
    running = next((s for s in steps if s.get("status") == "in_progress"), None)
    nxt = next((s for s in steps if s.get("status") == "pending"), None)
    tail = f"进度 {done}/{len(steps)}。"
    if running:
        tail += f"当前进行中：第 {running.get('index')} 步。"
    elif nxt:
        tail += f"下一步应从第 {nxt.get('index')} 步开始，先把它标 in_progress。"
    else:
        tail += "所有步骤已进入终态，若确已完成请做最终汇报。"
    lines.append(tail)
    if plan.get("pending_approval") and not plan.get("approved_at"):
        lines.append("该计划尚未获得用户批准，不要执行写操作。")
    if plan.get("replan_reason"):
        lines.append(f"⚠ 需要重新规划：{plan['replan_reason']}"
                     "。请先用 todo_write 修订计划，再继续执行。")
    text = "\n".join(lines)
    if len(text) > _NOTE_MAX_CHARS:
        text = text[:_NOTE_MAX_CHARS].rstrip() + "\n…（计划过长已截断）"
    return text


def render_human(session_id: str, *, plan: dict[str, Any] | None = None) -> str:
    """给人看的计划摘要（`/plan` 退出时、`ivyea plan show`）。"""
    plan = plan if plan is not None else load(session_id)
    if not plan:
        return "（当前会话没有计划）"
    from . import panels
    todos = [{"content": s.get("content", ""), "status": s.get("status", "pending")}
             for s in plan.get("steps") or []]
    body = panels.render_todos(todos, color=False) or "（计划为空）"
    head = []
    if plan.get("objective"):
        head.append(f"目标：{plan['objective']}")
    for item in plan.get("success_criteria") or []:
        head.append(f"完成标准：{item}")
    if plan.get("approved_at"):
        head.append(f"状态：已批准（{time.strftime('%Y-%m-%d %H:%M', time.localtime(plan['approved_at']))}）")
    elif plan.get("pending_approval"):
        head.append("状态：等待用户 /approve 批准")
    revisions = plan.get("revisions") or []
    tail = f"\n计划修订 {len(revisions)} 次。" if revisions else ""
    return ("\n".join(head) + "\n" if head else "") + body + tail
