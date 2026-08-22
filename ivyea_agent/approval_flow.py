"""审批 → 执行 → 回滚 的编排。把已建好的零件接成闭环。

这是**安全攸关**路径。七道闸（方案 §6.1）里，属于 agent 侧的四道全在这里：
  4. approval 状态机一次性消费（approvals.resolve 的原子 CAS）
  5. operate 写开关 + TTL（lingxing_write.operate_active）
  6. 幅度硬闸 ≤20%（lingxing_write.magnitude_ok）
  7. 写前快照 + 审计 + 回滚（lingxing_write 内建）
前三道（发送者白名单 / 回调 chat 一致性 / 卡片 token 去重）在 relay 侧，
因为只有那里拿得到飞书事件的 operator 与 open_chat_id。

状态语义：
- ``approved`` 但未执行 = **待执行**（写开关没开时停在这里，开了可重试）
- ``executed`` = 已写入，带 audit_id，可回滚
- ``failed``   = 写入失败（lingxing_write 会自动熔断关掉写开关）
"""
from __future__ import annotations

from typing import Any, Optional

from . import approvals, feishu_card


def _card_sender():
    from . import feishu_client, notify
    return feishu_client, notify


def _update_card(approval: Any, card: dict[str, Any]) -> bool:
    """原地替换卡片。发送失败不该让整个流程失败——写已经做了，卡片只是呈现。"""
    if not getattr(approval, "message_id", ""):
        return False
    feishu_client, _ = _card_sender()
    try:
        return feishu_client.update_card(approval.message_id, card)
    except Exception:                              # noqa: BLE001
        return False


def resolve(approval_id: str, choice: str, *, operator: str = "",
            chat_id: str = "", update_card: bool = True,
            execute: bool = True) -> dict[str, Any]:
    """消费一个审批。approve 时默认立刻执行。

    返回 {ok, state, detail, reason, audit_id, card}。``card`` 是**建议回填的卡片**，
    relay 可以把它同步返回给飞书（比二次调用 update 快，也更不容易掉）。
    """
    ok, appr, reason = approvals.resolve(approval_id, choice, operator=operator,
                                         chat_id=chat_id)
    if not ok:
        return {"ok": False, "reason": reason,
                "state": getattr(appr, "state", ""),
                "detail": _reason_text(reason)}

    if choice == "deny":
        card = feishu_card.build_resolved_card(choice="deny", operator=operator,
                                               preview=appr.preview)
        if update_card:
            _update_card(appr, card)
        return {"ok": True, "state": approvals.DENIED, "detail": "已忽略", "card": card}

    if not execute:
        card = feishu_card.build_resolved_card(choice="approve", operator=operator,
                                               preview=appr.preview)
        if update_card:
            _update_card(appr, card)
        return {"ok": True, "state": approvals.APPROVED, "detail": "已批准，待执行",
                "card": card}

    return execute_approved(approval_id, operator=operator, update_card=update_card)


def execute_approved(approval_id: str, *, operator: str = "",
                     update_card: bool = True) -> dict[str, Any]:
    """执行一个已批准的审批项。可重复调用（写开关补开后重试）。"""
    from . import lingxing_write

    appr = approvals.get(approval_id)
    if appr is None:
        return {"ok": False, "reason": "unknown", "detail": "审批项不存在"}
    if appr.state != approvals.APPROVED:
        return {"ok": False, "reason": "not_approved", "state": appr.state,
                "detail": f"当前状态 {appr.state}，不可执行"}
    intent = appr.intent or {}
    if not intent:
        approvals.mark_failed(approval_id, "审批项没有可执行 intent")
        return {"ok": False, "reason": "no_intent", "detail": "审批项没有可执行 intent"}

    # 闸 6：幅度硬闸。放在写开关之前 —— 幅度不合法就该直接判失败，
    # 而不是"等你开了开关再来撞一次"。
    passed, why = lingxing_write.magnitude_ok(intent)
    if not passed:
        approvals.mark_failed(approval_id, f"硬闸拦截：{why}")
        card = feishu_card.build_failed_card(preview=appr.preview,
                                             reason=f"硬闸拦截：{why}", operator=operator)
        if update_card:
            _update_card(appr, card)
        return {"ok": False, "reason": "guardrail", "detail": why,
                "state": approvals.FAILED, "card": card}

    # 闸 5：写开关。关着时**保持 approved**，不判失败 —— 用户的批准意愿仍然有效，
    # 开关补开后可以直接重试，不必重新发一遍卡片。
    if not lingxing_write.operate_active():
        detail = ("领星写开关未开启，已记为**待执行**。\n\n"
                  "点下面的按钮当场开启（带自动失效），或在终端执行："
                  "`ivyea lingxing operate on` 后 `ivyea approval execute <ID>`")
        card = feishu_card.build_operate_off_card(detail)
        if update_card:
            _update_card(appr, card)
        return {"ok": False, "reason": "operate_off", "state": approvals.APPROVED,
                "detail": detail, "card": card}

    # 闸 7：真实写入（内部抓快照 + 审计 + 失败熔断）
    try:
        result = lingxing_write.execute(intent, dry_run=False)
    except Exception as exc:                       # noqa: BLE001
        approvals.mark_failed(approval_id, f"执行异常：{exc}")
        card = feishu_card.build_failed_card(preview=appr.preview,
                                             reason=f"执行异常：{exc}", operator=operator)
        if update_card:
            _update_card(appr, card)
        return {"ok": False, "reason": "exception", "detail": str(exc),
                "state": approvals.FAILED, "card": card}

    if not result.get("ok"):
        detail = str(result.get("detail") or "写入失败")
        approvals.mark_failed(approval_id, detail)
        card = feishu_card.build_failed_card(preview=appr.preview, reason=detail,
                                             operator=operator)
        if update_card:
            _update_card(appr, card)
        return {"ok": False, "reason": "write_failed", "detail": detail,
                "state": approvals.FAILED, "card": card}

    audit_id = str(result.get("audit_id") or "")
    approvals.mark_executed(approval_id, audit_id=audit_id,
                            detail=str(result.get("detail") or ""))
    card = feishu_card.build_executed_card(
        preview=appr.preview, operator=operator, audit_id=audit_id,
        approval_id=approval_id, detail=str(result.get("detail") or ""))
    if update_card:
        _update_card(appr, card)
    return {"ok": True, "state": approvals.EXECUTED, "audit_id": audit_id,
            "detail": result.get("detail", ""), "card": card}


def rollback(approval_id: str, *, operator: str = "", chat_id: str = "",
             update_card: bool = True) -> dict[str, Any]:
    """回滚一条已执行的审批。"""
    from . import lingxing_write

    appr = approvals.get(approval_id)
    if appr is None:
        return {"ok": False, "reason": "unknown", "detail": "审批项不存在"}
    if appr.chat_id and chat_id and appr.chat_id != chat_id:
        return {"ok": False, "reason": "chat_mismatch", "detail": "会话不匹配"}
    if appr.state != approvals.EXECUTED:
        return {"ok": False, "reason": "not_executed", "state": appr.state,
                "detail": f"当前状态 {appr.state}，只有已执行的才能回滚"}
    if not appr.audit_id:
        return {"ok": False, "reason": "no_audit", "detail": "没有审计号，无从回滚"}

    try:
        result = lingxing_write.rollback(appr.audit_id)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "reason": "exception", "detail": f"回滚异常：{exc}"}
    if not result.get("ok"):
        return {"ok": False, "reason": "rollback_failed",
                "detail": str(result.get("detail") or "回滚失败")}

    detail = str(result.get("detail") or "已恢复原值")
    approvals.mark_rolled_back(approval_id, detail)
    card = feishu_card.build_rolled_back_card(preview=appr.preview, operator=operator,
                                              detail=detail)
    if update_card:
        _update_card(appr, card)
    return {"ok": True, "state": approvals.ROLLED_BACK, "detail": detail, "card": card}


_REASONS = {
    "unknown": "审批项不存在或已被清理",
    "already_resolved": "该操作已处理过（重复点击无效）",
    "expired": "审批已超过有效期，为安全起见不再执行",
    "chat_mismatch": "会话不匹配：这张卡片不属于当前会话",
}


def _reason_text(reason: str) -> str:
    return _REASONS.get(reason, reason or "未知原因")


def status(approval_id: str) -> Optional[dict[str, Any]]:
    a = approvals.get(approval_id)
    if a is None:
        return None
    return {"id": a.id, "state": a.state, "sid": a.sid, "code": a.code,
            "action_class": a.action_class, "target_id": a.target_id,
            "target_name": a.target_name, "preview": a.preview,
            "intent": a.intent, "evidence": a.evidence,
            "chat_id": a.chat_id, "message_id": a.message_id,
            "created_at": a.created_at, "expires_at": a.expires_at,
            "resolved_by": a.resolved_by, "audit_id": a.audit_id,
            "detail": a.detail}


# ── P6 打磨：卡片上的非审批动作 ─────────────────────────────────────────────
def approve_all(message_id: str, *, operator: str = "", chat_id: str = "",
                confirm: bool = False, limit: int = 20) -> dict[str, Any]:
    """批量批准同一张卡片上的待审批项。

    **必须二次确认**：一次点击执行 N 个写操作，风险与收益不对称。
    第一次点击只返回确认卡，第二次（confirm=True）才真执行。
    """
    pending = [a for a in approvals.list_items(state=approvals.PENDING, limit=200)
               if a.message_id == message_id]
    if chat_id:
        pending = [a for a in pending if not a.chat_id or a.chat_id == chat_id]
    pending = pending[:limit]
    if not pending:
        return {"ok": False, "reason": "nothing_pending",
                "detail": "这张卡片上没有待处理的项了",
                "card": feishu_card.build_text_card("ℹ️ 无待处理项",
                                                    "这张卡片上的建议都已经处理过了。")}

    if not confirm:
        lines = [f"即将批准 **{len(pending)}** 条动作：", ""]
        lines += [f"{i}. {a.preview}" for i, a in enumerate(pending, 1)]
        lines.append("")
        lines.append("确认后会依次执行。每条仍各自过幅度硬闸，执行后可单独回滚。")
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": f"⚠️ 确认批量批准 {len(pending)} 条"},
                       "template": "orange"},
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
                {"tag": "action", "actions": [
                    {"tag": "button", "type": "danger",
                     "text": {"tag": "plain_text", "content": f"确认批准 {len(pending)} 条"},
                     "value": {"ivyea_action": "approve_all_confirm",
                               "message_id": message_id}},
                ]},
            ],
        }
        return {"ok": True, "reason": "need_confirm", "count": len(pending),
                "detail": f"待确认 {len(pending)} 条", "card": card}

    results = []
    for a in pending:
        r = resolve(a.id, "approve", operator=operator, chat_id=chat_id,
                    update_card=False)
        results.append({"id": a.id, "ok": bool(r.get("ok")),
                        "state": r.get("state", ""), "detail": r.get("detail", ""),
                        "preview": a.preview})
    done = sum(1 for r in results if r["ok"])
    lines = [f"批量执行完成：成功 **{done}** / {len(results)}", ""]
    for r in results:
        mark = "✅" if r["ok"] else "⚠️"
        lines.append(f"{mark} {r['preview']}　— {r['detail'] or r['state']}")
    card = feishu_card.build_text_card(
        f"{'✅' if done == len(results) else '⚠️'} 批量执行 {done}/{len(results)}",
        "\n".join(lines), template="green" if done == len(results) else "orange")
    return {"ok": True, "count": len(results), "done": done,
            "results": results, "card": card}


def set_operate(minutes: int = 120, *, operator: str = "") -> dict[str, Any]:
    """开启领星写开关（带 TTL 自动失效）。

    做成卡片按钮是因为：用户在手机上收到告警、点了批准，却被"写开关未开"挡住时，
    唯一的出路不该是"你去登服务器敲命令"。
    """
    from . import lingxing_write

    minutes = max(1, min(int(minutes or 120), 480))
    lingxing_write.set_operate(True, ttl_minutes=minutes)
    detail = f"领星写开关已开启，{minutes} 分钟后自动关闭。"
    card = feishu_card.build_text_card(
        "🔓 写开关已开启",
        f"{detail}\n\n操作人：{operator or '未知'}\n\n"
        f"期间被批准的动作会真实写入领星；每条仍过幅度硬闸，执行后可回滚。",
        template="orange")
    return {"ok": True, "detail": detail, "expires_in_minutes": minutes, "card": card}


def operate_status() -> dict[str, Any]:
    from . import lingxing_write
    return {"ok": True, "active": bool(lingxing_write.operate_active())}
