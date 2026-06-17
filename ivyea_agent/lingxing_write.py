"""领星受控写入（agent 自有，独立实现）—— 安全攸关路径。

写入永不随意发生。每个写动作必须依次通过：
1. **人工审批**（permission.py 多档确认；agent 的把关方式，替代 ivyea-ops 的三重 LLM 复核）。
2. **确定性硬闸**（代码，非 LLM）：operate 开关已开 + 幅度 ≤ max_change_pct + 形状合法。
3. 仅当 dry_run=False 且开关开启才真正经领星 OpenAPI 写；写前抓回滚快照；全程审计。

OP_TYPES / build_body / 路由 / 回滚 逐字段对齐 ivyea-ops `lingxing_operate.py`（权威规格）。
默认 dry-run；operate 开关默认关。M2 支持：否词 / 关键词调bid / 活动预算。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from . import audit, config
from .lingxing_datasets import fetch_dataset
from .lingxing_openapi import LingXingError, call

MAX_CHANGE_PCT = 0.20  # 单次 ≤20%（与 guardrails 一致）

# 写操作注册（权威：ivyea-ops lingxing_operate.OP_TYPES 子集）
OP_TYPES: dict[str, dict[str, Any]] = {
    "campaign_budget": {
        "category": "modify", "label": "广告活动·预算", "route": "/basicOpen/adReport/manage/putSpCampaign",
        "array": "campaigns", "id_field": "campaignId", "num_field": "daily_budget",
        "snapshot_dataset": "sp_campaigns", "snapshot_id": "campaign_id", "snapshot_value": "daily_budget",
        "reversible": True,
    },
    "keyword_bid": {
        "category": "modify", "label": "关键词·竞价", "route": "/basicOpen/adReport/manage/putSpKeyword",
        "array": "keywords", "id_field": "keywordId", "num_field": "bid",
        "snapshot_dataset": "sp_keywords", "snapshot_id": "keyword_id", "snapshot_value": "bid",
        "reversible": True,
    },
    "negate_keyword": {
        "category": "add", "label": "否词(加否定关键词)", "route": "/basicOpen/adReport/spTarget/addNegativeKeywords",
        "body_key": "negativeKeywords", "archive_route": "/basicOpen/adReport/spTarget/archiveNegatives",
        "reversible": True,
    },
}


# ── operate 开关（确定性硬闸）─────────────────────────────────────────────────
def operate_active() -> bool:
    """领星写入总开关；默认关。带可选 TTL（lingxing_operate_expires_at）。"""
    if not config.get_setting("lingxing_operate_enabled", False):
        return False
    exp = config.get_setting("lingxing_operate_expires_at", 0)
    if exp and time.time() > float(exp):
        return False
    return True


def set_operate(on: bool, ttl_minutes: int = 120) -> None:
    config.set_setting("lingxing_operate_enabled", bool(on))
    config.set_setting("lingxing_operate_expires_at", time.time() + ttl_minutes * 60 if on else 0)


# ── 候选 → intent ────────────────────────────────────────────────────────────
def candidate_to_intent(cand: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把 optimizer 候选转成可写 intent；不可自动写的（收割/错误）返回 None。"""
    sid = cand.get("sid")
    op = cand.get("op_type")
    if op == "negate_keyword":
        return {"op_type": op, "sid": sid, "campaign_id": cand.get("campaign_id"),
                "ad_group_id": cand.get("ad_group_id"), "keyword_text": cand.get("target_name"),
                "match_type": "negativeExact"}
    if op == "keyword_bid":
        return {"op_type": op, "sid": sid, "target_id": cand.get("target_id"),
                "target_name": cand.get("target_name"),
                "change": {"bid": (cand.get("proposed") or {}).get("bid")},
                "before": {"bid": (cand.get("current") or {}).get("bid")}}
    if op == "campaign_budget":
        return {"op_type": op, "sid": sid, "target_id": cand.get("target_id"),
                "target_name": cand.get("target_name"),
                "change": {"daily_budget": (cand.get("proposed") or {}).get("daily_budget")},
                "before": {"daily_budget": (cand.get("current") or {}).get("daily_budget")}}
    return None  # add_keyword(收割) 为 advisory，不自动写


# ── 请求体构造（逐字段对齐 ivyea-ops build_body）──────────────────────────────
def build_body(intent: dict[str, Any]) -> dict[str, Any]:
    op = OP_TYPES.get(intent.get("op_type") or "")
    if not op:
        raise LingXingError(f"未知操作类型: {intent.get('op_type')}")
    if op["category"] == "add":
        item: dict[str, Any] = {"campaignId": str(intent["campaign_id"]),
                                "keyword": intent["keyword_text"],
                                "matchType": intent["match_type"], "state": "ENABLED"}
        if intent.get("ad_group_id"):
            item["adGroupId"] = str(intent["ad_group_id"])
        return {"sid": int(intent["sid"]), op["body_key"]: [item]}
    ch = intent.get("change") or {}
    nf = op["num_field"]
    item = {op["id_field"]: int(intent["target_id"]), "isBaseValue": 0}
    if ch.get("state"):
        item["state"] = ch["state"]
    if ch.get(nf) is not None:
        if nf == "daily_budget":
            item["budget"] = {"budgetType": "DAILY", "budget": float(ch[nf])}
        else:
            item[nf] = float(ch[nf])
    return {"sid": int(intent["sid"]), op["array"]: [item]}


# ── 幅度硬闸 ─────────────────────────────────────────────────────────────────
def magnitude_ok(intent: dict[str, Any]) -> tuple[bool, str]:
    op = OP_TYPES.get(intent.get("op_type") or "")
    if not op or op["category"] == "add":
        return True, ""
    nf = op["num_field"]
    new = (intent.get("change") or {}).get(nf)
    old = (intent.get("before") or {}).get(nf)
    if new is None or not old:
        return True, ""
    pct = abs(float(new) - float(old)) / float(old)
    if pct > MAX_CHANGE_PCT + 1e-9:
        return False, f"调整幅度 {pct:+.0%} 超过 ±{int(MAX_CHANGE_PCT*100)}% 上限"
    return True, ""


def preview(intent: dict[str, Any]) -> str:
    op = OP_TYPES.get(intent.get("op_type") or "", {})
    label = op.get("label", intent.get("op_type"))
    if op.get("category") == "add":
        return f"{label}「{intent.get('keyword_text')}」({intent.get('match_type')}) 活动 {intent.get('campaign_id')}"
    nf = op.get("num_field")
    old = (intent.get("before") or {}).get(nf)
    new = (intent.get("change") or {}).get(nf)
    name = intent.get("target_name") or intent.get("target_id")
    return f"{label} {name}：{old} → {new}"


# ── 写前快照（用于回滚）──────────────────────────────────────────────────────
def _current_value(intent: dict[str, Any]) -> dict[str, Any]:
    op = OP_TYPES.get(intent.get("op_type") or "", {})
    if op.get("category") == "add" or not op.get("snapshot_dataset"):
        return dict(intent.get("before") or {})
    ds, idf, vf = op["snapshot_dataset"], op["snapshot_id"], op["snapshot_value"]
    tid = str(intent.get("target_id"))
    try:
        for offset in range(0, 3000, 300):
            rows = fetch_dataset(ds, {"sid": int(intent["sid"]), "length": 300, "offset": offset})
            for c in rows:
                if str(c.get(idf)) == tid:
                    return {op["num_field"]: c.get(vf), "state": c.get("state")}
            if len(rows) < 300:
                break
    except LingXingError:
        pass
    return dict(intent.get("before") or {})


def _extract_target_ids(res: Any) -> list[str]:
    ids: list[str] = []
    data = (res or {}).get("data") if isinstance(res, dict) else None
    if isinstance(data, dict):
        for key in ("success", "successTargets", "results", "successKeywords"):
            v = data.get(key)
            if isinstance(v, list):
                for it in v:
                    tid = (it or {}).get("targetId") or (it or {}).get("keywordId")
                    if tid:
                        ids.append(str(tid))
    return ids


# ── 执行 ────────────────────────────────────────────────────────────────────
def execute(intent: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """执行一个写 intent。dry_run=True 仅返回将发送的 route+body，不写。
    真写需 operate 开关开启 + 幅度通过 + dry_run=False。返回 {ok, dry_run, detail, audit_id?}。"""
    op = OP_TYPES.get(intent.get("op_type") or "")
    if not op:
        return {"ok": False, "dry_run": dry_run, "detail": f"未知操作类型 {intent.get('op_type')}"}
    ok, why = magnitude_ok(intent)
    if not ok:
        return {"ok": False, "dry_run": dry_run, "detail": f"硬闸拦截：{why}"}
    body = build_body(intent)
    route = op["route"]
    if dry_run:
        return {"ok": True, "dry_run": True, "detail": f"[DRY-RUN] {preview(intent)}",
                "route": route, "body": body}
    if not operate_active():
        return {"ok": False, "dry_run": False,
                "detail": "operate 开关未开启（ivyea lingxing operate on）——拒绝真实写入"}
    snapshot = _current_value(intent)
    try:
        res = call(route, body, method="POST")
    except LingXingError as e:
        set_operate(False)  # 熔断：写失败自动关开关
        return {"ok": False, "dry_run": False, "detail": f"写入失败已熔断（已关 operate 开关）：{e}"}
    if op["category"] == "add":
        snapshot = {"target_ids": _extract_target_ids(res), "add": True}
    aid = audit.record({"source": "lingxing", "op_type": intent["op_type"], "sid": intent.get("sid"),
                        "kind": op["label"], "search_term": intent.get("keyword_text") or intent.get("target_name"),
                        "intent": intent, "snapshot": snapshot, "route": route, "result": res})
    # 记忆：本店该目标已批准执行（支撑冷却/历史）
    try:
        from . import memory
        memory.record_decision(f"sid:{intent.get('sid')}",
                               intent.get("keyword_text") or str(intent.get("target_name") or intent.get("target_id")),
                               _kind_for_memory(intent["op_type"]), "approve")
    except Exception:
        pass
    return {"ok": True, "dry_run": False, "audit_id": aid, "detail": f"已执行：{preview(intent)}（审计 {aid}）",
            "result": res}


def _kind_for_memory(op_type: str) -> str:
    return {"negate_keyword": "negative", "keyword_bid": "reduce_bid",
            "campaign_budget": "budget"}.get(op_type, op_type)


# ── 回滚 ────────────────────────────────────────────────────────────────────
def rollback(audit_id: str) -> dict[str, Any]:
    entry = audit.get(audit_id)
    if not entry or entry.get("source") != "lingxing":
        return {"ok": False, "detail": f"未找到领星审计记录 {audit_id}"}
    intent = entry.get("intent") or {}
    snap = entry.get("snapshot") or {}
    op = OP_TYPES.get(intent.get("op_type") or "")
    if not op or not op.get("reversible"):
        return {"ok": False, "detail": "该操作不支持一键回滚"}
    if not operate_active():
        return {"ok": False, "detail": "operate 开关未开启，无法回滚"}
    try:
        if op["category"] == "add":
            tids = snap.get("target_ids") or []
            if not tids:
                return {"ok": False, "detail": "无可归档的 targetId（执行响应未返回ID，请到领星后台手动归档）"}
            res = call(op["archive_route"], {"sid": int(intent["sid"]), "targetIds": tids}, method="POST")
        else:
            nf = op["num_field"]
            change = {}
            if snap.get(nf) is not None:
                change[nf] = snap[nf]
            if snap.get("state"):
                change["state"] = snap["state"]
            body = build_body({"op_type": intent["op_type"], "sid": intent["sid"],
                               "target_id": intent["target_id"], "change": change})
            res = call(op["route"], body, method="POST")
    except LingXingError as e:
        return {"ok": False, "detail": f"回滚写入失败：{e}"}
    audit.record({"source": "lingxing", "kind": "rollback", "rollback_of": audit_id,
                  "sid": intent.get("sid"), "result": res})
    return {"ok": True, "detail": f"已回滚 {audit_id}：{preview(intent)}"}
