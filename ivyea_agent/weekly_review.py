"""Weekly/periodic operations review report."""
from __future__ import annotations

import time
from typing import Any

from . import action_queue, memory, scorecard, shadow, traces


def build(limit: int = 200) -> dict[str, Any]:
    q = action_queue.summary()
    items = action_queue.list_items(limit=limit)
    runs = memory.recent_runs(limit=8)
    sc = scorecard.build(limit=limit)
    tr = traces.stats(limit=limit)
    shadow_recs = shadow.list_recs(limit=limit)
    blocked = [i for i in items if i.get("blocked")]
    pending = [i for i in items if i.get("status") == "pending" and not i.get("blocked")]
    approved = [i for i in items if i.get("status") == "approved"]
    done = [i for i in items if i.get("status") == "done"]

    priorities = []
    if pending:
        priorities.append(f"复核 pending 动作 {len(pending)} 条。")
    if approved:
        priorities.append(f"执行或标记 approved 动作 {len(approved)} 条。")
    if blocked:
        priorities.append(f"复盘护栏拦截 {len(blocked)} 条，确认画像/保护词是否合理。")
    if tr.get("failures"):
        priorities.append(f"排查工具失败 {tr['failures']} 次。")
    if shadow_recs:
        priorities.append(f"查看影子台账 {len(shadow_recs)} 条，必要时跑 shadow report。")
    if not priorities:
        priorities.append("本地队列和运行状态无明显阻塞，继续按巡检节奏推进。")

    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "queue": q,
        "items": items,
        "blocked": blocked,
        "pending": pending,
        "approved": approved,
        "done": done,
        "recent_runs": runs,
        "scorecard": sc,
        "trace": tr,
        "shadow_recs": len(shadow_recs),
        "priorities": priorities,
    }


def _item_line(item: dict[str, Any]) -> str:
    payload = item.get("payload") or {}
    term = payload.get("search_term") or payload.get("target_name") or item.get("id")
    kind = payload.get("kind") or payload.get("op_type") or "-"
    reason = item.get("blocked_reason") or payload.get("block_reason") or payload.get("reason") or ""
    return f"- {item.get('id')} [{item.get('status')}] {kind} {term}" + (f" · {reason}" if reason else "")


def render(report: dict[str, Any]) -> str:
    q = report["queue"]
    sc = report["scorecard"]
    lines = [
        "# Ivyea 周期运营复盘",
        "",
        f"- 生成时间：{report['ts']}",
        f"- 动作队列：pending {q.get('pending', 0)} / approved {q.get('approved', 0)} / done {q.get('done', 0)} / blocked {q.get('blocked', 0)}",
        f"- 采纳率：{sc.get('approval_rate', 0):.0%}；执行率：{sc.get('execution_rate', 0):.0%}",
        f"- 工具调用：{report['trace'].get('tool_calls', 0)} 次；失败 {report['trace'].get('failures', 0)} 次",
        f"- 影子台账：{report['shadow_recs']} 条",
        "",
        "## 本周期优先事项",
    ]
    lines.extend(f"- {p}" for p in report["priorities"])
    lines.append("")
    lines.append("## 最近巡检")
    if not report["recent_runs"]:
        lines.append("（无）")
    else:
        for r in report["recent_runs"]:
            lines.append(
                f"- {time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} {r.get('asin') or '-'}："
                f"否 {r.get('negatives', 0)} / 放 {r.get('scale', 0)} / 降 {r.get('reduce', 0)}"
            )
    lines.append("")
    lines.append("## 待处理动作")
    rows = report["pending"][:10] + report["approved"][:10]
    if not rows:
        lines.append("（无）")
    else:
        lines.extend(_item_line(i) for i in rows[:20])
    lines.append("")
    lines.append("## 护栏拦截")
    if not report["blocked"]:
        lines.append("（无）")
    else:
        lines.extend(_item_line(i) for i in report["blocked"][:12])
    lines.append("")
    lines.append("## 下一步建议")
    lines.append("- 跑最新广告巡检，先处理 pending/approved 队列。")
    lines.append("- 对高点击低转化词，先用 listing/review/offer/competitor audit 判断承接原因。")
    lines.append("- 若影子台账有积累，跑 `ivyea shadow report --sid <SID>` 验证建议收益。")
    return "\n".join(lines) + "\n"
