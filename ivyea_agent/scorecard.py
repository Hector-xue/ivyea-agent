"""Operational scorecard for Ivyea Agent usage quality."""
from __future__ import annotations

import time
from typing import Any

from . import action_queue, memory, shadow, traces


def build(limit: int = 1000) -> dict[str, Any]:
    items = action_queue.list_items(limit=limit)
    queue = action_queue.summary()
    non_blocked = [i for i in items if not i.get("blocked")]
    approved = [i for i in non_blocked if i.get("status") in ("approved", "done")]
    done = [i for i in non_blocked if i.get("status") == "done"]
    denied = [i for i in non_blocked if i.get("status") == "denied"]
    actionable = len(non_blocked)
    memory_stats = memory.stats()
    shadow_recs = shadow.list_recs(limit=limit)
    trace_stats = traces.stats(limit=limit)
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "queue": queue,
        "actionable": actionable,
        "approval_rate": len(approved) / actionable if actionable else 0.0,
        "execution_rate": len(done) / actionable if actionable else 0.0,
        "denial_rate": len(denied) / actionable if actionable else 0.0,
        "memory": memory_stats,
        "recent_runs": memory.recent_runs(limit=8),
        "shadow_mode": shadow.shadow_mode(),
        "shadow_recs": len(shadow_recs),
        "traces": trace_stats,
    }


def _pct(value: float) -> str:
    return f"{value:.0%}"


def render_md(score: dict[str, Any]) -> str:
    q = score["queue"]
    m = score["memory"]
    lines = [
        "# Ivyea Agent 运营 Scorecard",
        "",
        f"- 生成时间：{score['ts']}",
        f"- 动作队列：pending {q.get('pending', 0)} / approved {q.get('approved', 0)} / "
        f"denied {q.get('denied', 0)} / done {q.get('done', 0)} / blocked {q.get('blocked', 0)}",
        f"- 建议采纳率：{_pct(score['approval_rate'])}",
        f"- 执行完成率：{_pct(score['execution_rate'])}",
        f"- 人工否决率：{_pct(score['denial_rate'])}",
        f"- 记忆决策：{m.get('decisions', 0)} 条（批准 {m.get('approved', 0)} / 否决 {m.get('rejected', 0)}）",
        f"- 巡检次数：{m.get('runs', 0)}",
        f"- 影子模式：{'开' if score.get('shadow_mode') else '关'}，台账建议 {score.get('shadow_recs', 0)} 条",
        f"- 工具调用：{score.get('traces', {}).get('tool_calls', 0)} 次，失败 {score.get('traces', {}).get('failures', 0)} 次，"
        f"平均 {score.get('traces', {}).get('avg_tool_ms', 0)}ms",
        "",
        "## 最近巡检",
    ]
    if not score["recent_runs"]:
        lines.append("（无）")
    else:
        for r in score["recent_runs"]:
            lines.append(
                f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['ts']))} "
                f"{r.get('asin') or '-'}：否词 {r.get('negatives', 0)} / "
                f"放量 {r.get('scale', 0)} / 降 bid {r.get('reduce', 0)}"
            )
    lines.extend([
        "",
        "## 解读",
        "- 采纳率低：优先检查画像、保护词、目标 ACOS 和建议解释是否贴合账户打法。",
        "- 护栏拦截高：说明规则在保护账户，需复核是否过度生成高风险动作。",
        "- done 低于 approved：说明审核后仍缺执行链路或执行节奏，需要处理队列。",
        "- 影子台账增长但无回测：说明需要后续真实数据来验证“若照做”的收益。",
    ])
    return "\n".join(lines) + "\n"
