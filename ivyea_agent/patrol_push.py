"""巡检结果 → 飞书卡片 → 审批项。把三个已建好的零件接起来。

刻意独立成模块，让 ``store_health`` 保持纯粹（只判断，不发送），
``feishu_card`` 保持纯函数（只构建，不联网），``approvals`` 只管状态机。
"""
from __future__ import annotations

from typing import Any

from . import approvals, feishu_card, notify


def _key(finding: Any) -> str:
    return f"{getattr(finding, 'target_id', '')}|{getattr(finding, 'code', '')}"


def push_result(result: Any, *, chat_id: str = "", store_name: str = "",
                channel: str = "feishu_app", ttl_seconds: float = approvals.DEFAULT_TTL_SECONDS,
                max_items: int = 10) -> dict[str, Any]:
    """把一次巡检推成一张卡片。

    只为**带 intent 的** Finding 建审批项——纯告警不该出现批准按钮。
    卡片发出后回填 message_id，供点击回调时原地更新。
    """
    findings = result.sorted_findings() if hasattr(result, "sorted_findings") else list(result.findings)
    created: dict[str, str] = {}
    ids: dict[str, str] = {}

    for f in findings[:max_items]:
        if not getattr(f, "intent", None):
            continue
        try:
            a = approvals.create(f, chat_id=chat_id, ttl_seconds=ttl_seconds)
        except approvals.ApprovalError:
            continue
        ids[_key(f)] = a.id
        created[a.id] = getattr(f, "code", "")

    card = feishu_card.build_alert_card(
        findings, sid=getattr(result, "sid", ""), store_name=store_name,
        layer=getattr(result, "layer", ""), approval_ids=ids, max_items=max_items)

    if channel == "feishu_app":
        from . import store_health
        # 走降级链：卡片发不出去时退到群机器人 webhook，告警不能就这么没了
        sent = notify.send_alert(store_health.render(result), card=card,
                                 chat_id=chat_id, title="店铺异常")
    else:
        from . import store_health
        sent = notify.send(store_health.render(result),
                           title=f"店铺巡检 {getattr(result, 'layer', '')}", channel=channel)

    message_id = str(sent.get("message_id") or "")
    if sent.get("ok") and message_id:
        for aid in created:
            approvals.set_message(aid, sent.get("chat_id") or chat_id, message_id)

    return {"ok": bool(sent.get("ok")), "message_id": message_id,
            "approvals": list(created), "findings": len(findings),
            "error": sent.get("error", ""), "channel": channel}


def push_daily(result: Any, *, date: str, store_name: str, metrics_lines: Any = (),
               chat_id: str = "", report_url: str = "",
               ttl_seconds: float = approvals.DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    """早报卡片。无异常时也发——用户要的就是每天确认一眼。"""
    findings = result.sorted_findings() if hasattr(result, "sorted_findings") else list(result.findings)
    created: dict[str, str] = {}
    ids: dict[str, str] = {}
    for f in findings:
        if not getattr(f, "intent", None):
            continue
        try:
            a = approvals.create(f, chat_id=chat_id, ttl_seconds=ttl_seconds)
        except approvals.ApprovalError:
            continue
        ids[_key(f)] = a.id
        created[a.id] = getattr(f, "code", "")

    card = feishu_card.build_daily_card(
        date=date, store_name=store_name, metrics_lines=metrics_lines,
        findings=findings, gaps=getattr(result, "gaps", []),
        approval_ids=ids, report_url=report_url)
    from . import store_health
    sent = notify.send_alert("\n".join(metrics_lines) or store_health.render(result),
                             card=card, chat_id=chat_id, title=f"店铺日报 {date}")
    message_id = str(sent.get("message_id") or "")
    if sent.get("ok") and message_id:
        for aid in created:
            approvals.set_message(aid, sent.get("chat_id") or chat_id, message_id)
    return {"ok": bool(sent.get("ok")), "message_id": message_id,
            "approvals": list(created), "error": sent.get("error", "")}
