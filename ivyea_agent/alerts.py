"""Local operational alerts for Ivyea Agent."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from . import action_queue, knowledge, knowledge_governance, profiles, shadow, traces


def _sev(rank: str) -> int:
    return {"info": 0, "warn": 1, "fail": 2}.get(rank, 0)


def check(*, limit: int = 500) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    q = action_queue.summary()
    if q.get("approved", 0):
        alerts.append({
            "severity": "warn",
            "code": "queue.approved_pending_execution",
            "message": f"{q['approved']} 条动作已批准但未完成执行。",
            "fix": "运行 `ivyea action list --status approved` 复核，必要时 `ivyea action execute --id <ID>`。",
        })
    if q.get("pending", 0) > 20:
        alerts.append({
            "severity": "warn",
            "code": "queue.pending_backlog",
            "message": f"pending 动作积压 {q['pending']} 条。",
            "fix": "运行 `ivyea action report --status pending` 批量复核。",
        })
    if q.get("blocked", 0) > 20:
        alerts.append({
            "severity": "info",
            "code": "queue.blocked_many",
            "message": f"护栏拦截 {q['blocked']} 条，可能是画像/保护词过严或建议偏激。",
            "fix": "运行 `ivyea action list --status blocked` 检查原因。",
        })

    tr = traces.stats(limit=limit)
    if tr.get("failures", 0):
        alerts.append({
            "severity": "warn",
            "code": "trace.failures",
            "message": f"最近 {limit} 条运行事件中有 {tr['failures']} 次失败。",
            "fix": "运行 `ivyea trace recent --limit 50` 查看失败工具。",
        })

    shadow_count = len(shadow.list_recs(limit=limit))
    if shadow_count:
        alerts.append({
            "severity": "info",
            "code": "shadow.review_due",
            "message": f"影子台账有 {shadow_count} 条建议，可回测建立信任。",
            "fix": "运行 `ivyea shadow report --sid <SID>`。",
        })

    user_cards = knowledge.list_user_cards()
    if not user_cards:
        alerts.append({
            "severity": "info",
            "code": "knowledge.no_user_cards",
            "message": "尚未导入用户知识卡。",
            "fix": "运行 `ivyea knowledge import ./note.md --id user.note` 沉淀你的打法。",
        })

    governance = knowledge_governance.dashboard()
    gov = governance["summary"]
    if gov["pending_reviews"]:
        alerts.append({
            "severity": "warn",
            "code": "knowledge.review_backlog",
            "message": f"官方来源有 {gov['pending_reviews']} 条变更等待审核。",
            "fix": "运行 `ivyea knowledge changes --status pending`，再用 `knowledge review` 审核。",
        })
    if gov["approved_not_published"]:
        alerts.append({
            "severity": "info",
            "code": "knowledge.approved_not_published",
            "message": f"有 {gov['approved_not_published']} 条来源变更已批准，但尚未生成并确认知识更新。",
            "fix": "根据快照生成 `knowledge plan` 草案，复核后再 `knowledge apply --confirm`。",
        })
    if gov["stale_cards"]:
        alerts.append({
            "severity": "warn",
            "code": "knowledge.stale_cards",
            "message": f"知识库有 {gov['stale_cards']} 张过期卡需要复核。",
            "fix": "运行 `ivyea knowledge governance` 查看过期卡和对应官方来源。",
        })
    if gov["monitor_errors"]:
        alerts.append({
            "severity": "warn",
            "code": "knowledge.monitor_errors",
            "message": f"官方来源监控有 {gov['monitor_errors']} 个错误。",
            "fix": "运行 `ivyea knowledge sync-status` 查看错误并重试对应来源。",
        })
    if gov["monitor_overdue"]:
        alerts.append({
            "severity": "warn",
            "code": "knowledge.monitor_overdue",
            "message": f"官方来源监控有 {gov['monitor_overdue']} 个来源超过计划周期。",
            "fix": "运行 `ivyea knowledge sync` 或检查 knowledge_sync 计划任务。",
        })
    if gov["conflicts"]:
        alerts.append({
            "severity": "warn",
            "code": "knowledge.conflicts",
            "message": f"检测到 {gov['conflicts']} 条知识冲突或证据边界风险。",
            "fix": "运行 `ivyea knowledge conflicts` 逐条复核。",
        })
    if gov["coverage_gaps"]:
        alerts.append({
            "severity": "info",
            "code": "knowledge.coverage_gaps",
            "message": f"关键知识域/站点矩阵仍有 {gov['coverage_gaps']} 个缺口。",
            "fix": "运行 `ivyea knowledge coverage`，按风险和站点补充官方知识。",
        })

    profs = profiles.list_profiles()
    configured = [name for name, p in profs if p.get("target_acos") is not None and p.get("protected_terms")]
    if not configured:
        alerts.append({
            "severity": "warn",
            "code": "profile.incomplete",
            "message": "未发现同时配置目标 ACOS 和保护词的运营画像。",
            "fix": "运行 `ivyea profile set default --target-acos 0.3 --protected 品牌词`。",
        })

    usage = shutil.disk_usage(str(Path.home()))
    free_gb = usage.free / (1024 ** 3)
    used_pct = usage.used / usage.total
    if free_gb < 2 or used_pct > 0.95:
        alerts.append({
            "severity": "fail",
            "code": "system.disk_critical",
            "message": f"磁盘空间紧张：可用 {free_gb:.1f}G，已用 {used_pct:.0%}。",
            "fix": "清理缓存、日志、构建产物或容器镜像。",
        })
    elif free_gb < 8 or used_pct > 0.85:
        alerts.append({
            "severity": "warn",
            "code": "system.disk_low",
            "message": f"磁盘空间偏低：可用 {free_gb:.1f}G，已用 {used_pct:.0%}。",
            "fix": "建议保留 8G+ 可用空间。",
        })

    alerts.sort(key=lambda a: (-_sev(a["severity"]), a["code"]))
    return alerts


def render(alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return "Ivyea Alerts\n\nOK 无预警。\n"
    icon = {"info": "INFO", "warn": "WARN", "fail": "FAIL"}
    lines = ["Ivyea Alerts", ""]
    for a in alerts:
        lines.append(f"- {icon.get(a['severity'], 'INFO')} {a['code']}: {a['message']}")
        if a.get("fix"):
            lines.append(f"  修复: {a['fix']}")
    return "\n".join(lines) + "\n"
