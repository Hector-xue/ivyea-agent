"""目标台账：目标模式下"什么算完成"这件事的唯一权威记录。

为什么要单独一层
----------------
目标模式的承诺是「一句话交出去，达成之前不停」。这句承诺只有在**完成标准是确定性
的、落盘的、跨压缩不丢的**时候才成立 —— 否则跑到第 80 步，上下文一压缩，模型早忘了
自己最初答应过什么，然后心满意足地宣布完成。这与 `plan_store` 踩过的是同一个坑，
所以这里照抄它那套结论：**model-driven 的东西必须改成 runtime-driven**。

它和计划台账（`plan_store`）的分工
----------------------------------
* `plan_store` 记的是**怎么做** —— 步骤、进度、改过几版。步骤做完了是事实。
* `goal_store` 记的是**做到什么程度算数** —— 验收标准、逐条判定、判定依据。
  步骤全做完 ≠ 目标达成，这正是目标模式存在的理由。

降级契约（与 plan_store 逐字相同）
----------------------------------
**没有 session_id 就整个模块空转**：`start` 返回 None，`render_note` 返回空串。
落盘失败一律吞掉 —— 目标台账是增益，不能成为"活干不下去"的新理由。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import config, security

GOAL_NOTE_MARKER = "[目标契约]"

#: 逐条验收标准的状态。`unverifiable` 是刻意留的第三态 —— 把"没法验"混进"没达成"
#: 会让门禁把模型钉死在一条它永远过不去的标准上，混进"达成"则是自欺。
STATUSES = ("pending", "met", "unmet", "unverifiable")
TERMINAL_STATUSES = {"met", "unverifiable"}

#: 注回上下文的目标文本上限。它**每轮常驻**，和计划一样是要按轮付费的。
_NOTE_MAX_CHARS = 1400
_CRITERION_MAX_CHARS = 200
_MAX_CRITERIA = 12          # 验收标准超过这个数就不是一个目标，是一张需求清单
_MAX_ROUNDS = 20            # 判定留痕保留多少轮


def goals_dir() -> Path:
    return config.IVYEA_DIR / "goals"


def _now() -> float:
    return time.time()


def _safe_key(session_id: str) -> str:
    """session_id → 文件名。会话 id 来自上游（serve/飞书/CLI），不能直接当路径用。"""
    key = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(session_id or ""))
    return key[:120]


def path_for(session_id: str) -> Path | None:
    key = _safe_key(session_id)
    return (goals_dir() / f"{key}.json") if key else None


def load(session_id: str) -> dict[str, Any] | None:
    """读目标。不存在/损坏一律当作没有 —— 绝不让一个坏文件把对话卡死。"""
    path = path_for(session_id)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(goal: dict[str, Any]) -> dict[str, Any]:
    """原子落盘。写失败只吞掉不抛。"""
    path = path_for(goal.get("session_id", ""))
    if path is None:
        return goal
    goal["updated_at"] = _now()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".goal-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(goal, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        return goal
    return goal


def delete(session_id: str) -> bool:
    path = path_for(session_id)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _clip(text: Any, limit: int) -> str:
    clean = " ".join(security.redact_text(str(text or "")).split())
    return clean[:limit]


def _norm_criteria(items: Any) -> list[dict[str, Any]]:
    """把模型给的验收标准规整成台账里的行。

    只认两种形状：字符串，或 {text, verify}。别的一律丢掉 —— 台账里放一条读不懂的
    标准，等于给门禁埋了一条永远判不了的条目。
    """
    rows: list[dict[str, Any]] = []
    for raw in (items or []):
        if isinstance(raw, str):
            text, verify = raw, ""
        elif isinstance(raw, dict):
            text = raw.get("text") or raw.get("criterion") or raw.get("content") or ""
            verify = raw.get("verify") or raw.get("verification") or raw.get("how") or ""
        else:
            continue
        text = _clip(text, _CRITERION_MAX_CHARS)
        if not text:
            continue
        rows.append({
            "index": len(rows) + 1,
            "text": text,
            "verify": _clip(verify, _CRITERION_MAX_CHARS),
            "status": "pending",
            "reason": "",
            "evidence": "",
            "checked_at": 0.0,
        })
        if len(rows) >= _MAX_CRITERIA:
            break
    return rows


def same_query(goal: dict[str, Any] | None, query: str) -> bool:
    """这句指令和契约立约时的那句是不是同一句。

    比较前要走一遍和落盘时**同一套**归一化（脱敏 + 压空白 + 截断），否则原文与
    盘上那份永远不相等，每一轮都会重新立约 —— 而重新立约会把已经判过的进度抹掉。
    """
    return bool(goal) and str(goal.get("query") or "") == _clip(query, 600)


def start(session_id: str, *, query: str = "", objective: str = "",
          criteria: Any = None, out_of_scope: Any = None, risks: Any = None,
          task_id: str = "", derived_by: str = "") -> dict[str, Any] | None:
    """立一份新的目标契约（覆盖同会话的旧契约）。没有 session_id 就空转返回 None。"""
    if not _safe_key(session_id):
        return None
    goal = {
        "id": _safe_key(session_id),
        "session_id": session_id,
        "task_id": task_id or "",
        "query": _clip(query, 600),
        "objective": _clip(objective, 400),
        "criteria": _norm_criteria(criteria),
        "out_of_scope": [_clip(x, 160) for x in (out_of_scope or [])][:6],
        "risks": [_clip(x, 160) for x in (risks or [])][:6],
        "rounds": [],
        "status": "active",          # active | achieved | stopped
        "stop_reason": "",
        "derived_by": derived_by or "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    return save(goal)


def record_judgment(session_id: str, verdict: dict[str, Any]) -> dict[str, Any] | None:
    """把一次验收判定写进台账，返回更新后的目标。

    判定按 `index` 对齐 —— 模型漏判的条目**保持原状**，不会因为它这次没提就被抹成
    未达成（那会让已经验过的东西反复重验）。
    """
    goal = load(session_id)
    if not goal:
        return None
    by_index = {}
    for row in (verdict.get("criteria") or []):
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if idx:
            by_index[idx] = row
    for item in goal.get("criteria") or []:
        row = by_index.get(int(item.get("index") or 0))
        if not row:
            continue
        status = str(row.get("status") or "").strip().lower()
        if status not in STATUSES:
            continue
        item["status"] = status
        item["reason"] = _clip(row.get("reason"), 240)
        item["evidence"] = _clip(row.get("evidence"), 240)
        item["checked_at"] = _now()
    rounds = list(goal.get("rounds") or [])
    rounds.append({
        "at": _now(),
        "achieved": bool(verdict.get("achieved")),
        "unmet": [int(i.get("index") or 0) for i in (goal.get("criteria") or [])
                  if i.get("status") == "unmet"],
        "note": _clip(verdict.get("note") or verdict.get("summary"), 300),
    })
    goal["rounds"] = rounds[-_MAX_ROUNDS:]
    if achieved(goal):
        goal["status"] = "achieved"
    return save(goal)


def achieved(goal: dict[str, Any] | None) -> bool:
    """所有标准都进终态才算达成。一条 pending 都不许剩 —— 没判过不等于做到了。"""
    items = (goal or {}).get("criteria") or []
    if not items:
        return False
    return all(str(i.get("status")) in TERMINAL_STATUSES for i in items)


def unmet(goal: dict[str, Any] | None) -> list[dict[str, Any]]:
    """还没达成的标准（含从未判定过的 pending）。"""
    return [i for i in ((goal or {}).get("criteria") or [])
            if str(i.get("status")) not in TERMINAL_STATUSES]


def fingerprint(goal: dict[str, Any] | None) -> str:
    """当前未达成集合的指纹。连着两轮一模一样 = 判定没往前走（供无进展熔断用）。"""
    return ",".join(str(i.get("index")) + ":" + str(i.get("status"))
                    for i in unmet(goal))


def stop(session_id: str, reason: str = "") -> dict[str, Any] | None:
    """收摊（用户喊停 / 预算见底 / 无进展熔断）。目标本身留着，供下次续跑。"""
    goal = load(session_id)
    if not goal:
        return None
    if goal.get("status") == "active":
        goal["status"] = "stopped"
    goal["stop_reason"] = _clip(reason, 300)
    return save(goal)


def resume(session_id: str) -> dict[str, Any] | None:
    """把停下的目标接回来（用户说"继续"）。

    没有这一步，"剩余标准留在目标台账里，说继续可以接着做"就是一句空话：台账还在，
    但状态是 stopped，门禁一律不生效 —— 续跑跑的是普通轮次，用户不会知道。
    """
    goal = load(session_id)
    if not goal or goal.get("status") != "stopped":
        return goal
    goal["status"] = "active"
    goal["stop_reason"] = ""
    return save(goal)


_MARK = {"met": "✓", "unmet": "✗", "unverifiable": "◌", "pending": "·"}


def render_note(session_id: str, *, goal: dict[str, Any] | None = None) -> str:
    """渲染注回模型上下文的 `[目标契约]` 段。没有目标时返回空串。"""
    goal = goal if goal is not None else load(session_id)
    if not goal or not (goal.get("criteria") or []):
        return ""
    lines = [GOAL_NOTE_MARKER + " 目标模式进行中。这份契约由运行时保管，跨上下文压缩不丢；"
             "**判定权不在你手上** —— 你说完成不算完成，逐条验收通过才算。"]
    if goal.get("objective"):
        lines.append(f"目标：{goal['objective']}")
    lines.append("验收标准（每条都必须有真实证据）：")
    for item in goal.get("criteria") or []:
        mark = _MARK.get(str(item.get("status")), "·")
        line = f"{mark} {item.get('index')}. {item.get('text')}"
        if item.get("verify"):
            line += f"｜验证方式：{item['verify']}"
        if item.get("status") == "unmet" and item.get("reason"):
            line += f"｜上轮判定未达成：{item['reason']}"
        lines.append(line)
    if goal.get("out_of_scope"):
        lines.append("不在范围内（不要顺手做）：" + "；".join(goal["out_of_scope"]))
    left = unmet(goal)
    if left:
        lines.append(f"还剩 {len(left)}/{len(goal.get('criteria') or [])} 条未达成："
                     + "、".join(f"第{i.get('index')}条" for i in left)
                     + "。继续干活，不要在这里收尾。")
    else:
        lines.append("全部标准已进终态，可以做最终汇总收尾。")
    text = "\n".join(lines)
    if len(text) > _NOTE_MAX_CHARS:
        text = text[:_NOTE_MAX_CHARS].rstrip() + "\n…（目标契约过长已截断）"
    return text


def render_human(session_id: str, *, goal: dict[str, Any] | None = None) -> str:
    """给人看的目标摘要（CLI 的 `/goal show`）。"""
    goal = goal if goal is not None else load(session_id)
    if not goal:
        return "（当前会话没有目标契约）"
    head = [f"目标：{goal.get('objective') or goal.get('query') or '（未记录）'}"]
    state = {"active": "进行中", "achieved": "已达成", "stopped": "已停止"}.get(
        str(goal.get("status")), str(goal.get("status")))
    head.append(f"状态：{state}" + (f"（{goal['stop_reason']}）" if goal.get("stop_reason") else ""))
    body = []
    for item in goal.get("criteria") or []:
        mark = _MARK.get(str(item.get("status")), "·")
        body.append(f"  {mark} {item.get('index')}. {item.get('text')}")
        if item.get("reason"):
            body.append(f"       判定：{item['reason']}")
    if not body:
        body = ["  （没有验收标准）"]
    rounds = goal.get("rounds") or []
    tail = f"\n已判定 {len(rounds)} 轮。" if rounds else ""
    return "\n".join(head) + "\n验收标准：\n" + "\n".join(body) + tail


def public_state(session_id: str, *, goal: dict[str, Any] | None = None) -> dict[str, Any]:
    """给界面（IvyeaOps 任务台）的确定性投影。**别让界面自己去猜进度**。"""
    goal = goal if goal is not None else load(session_id)
    if not goal:
        return {}
    items = goal.get("criteria") or []
    return {
        "objective": goal.get("objective", ""),
        "status": goal.get("status", "active"),
        "stop_reason": goal.get("stop_reason", ""),
        "criteria": [{"index": i.get("index"), "text": i.get("text"),
                      "verify": i.get("verify", ""), "status": i.get("status"),
                      "reason": i.get("reason", "")} for i in items],
        "out_of_scope": list(goal.get("out_of_scope") or []),
        "met": sum(1 for i in items if i.get("status") == "met"),
        "total": len(items),
        "rounds": len(goal.get("rounds") or []),
    }
