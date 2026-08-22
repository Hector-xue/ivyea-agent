"""店铺业务健康巡检 —— 「昨天/刚才出什么事了」。

与 ``alerts.py`` 的分工：alerts 检的是 **agent 自身**健康（队列积压、trace 失败）；
本模块检的是 **店铺业务**异常。与 ``lingxing_optimizer`` 的分工：优化器回答
"能优化什么"（慢变量、14 天窗口），本模块回答"出事了吗"（快变量）。

分层（ADR-9：巡检频率由数据新鲜度决定，不由重要性决定）：
- **L1 快照层**：无时间窗的指标（库存量、活动/关键词配置）。当前走轮询，
  P7 接入 SP-API 推送后同一批规则自动升级为秒级——规则代码不变（ADR-8 的收益）。
- L2 日内层 / L3 隔日层：见 P1b / P1c。

证据门槛按**动作可逆性**分档（ADR-7），不按统一阈值：
- ``STANCH`` 止血型（降预算/降 bid/暂停）：完全可逆、影响有界 → 单点异常即可建议。
- ``STRUCTURAL`` 结构型（否词/加词）：不可逆 → 需统计显著性，只在 L3 出。
- ``ADVISORY`` 建议型（补货/改价）：本期无写通道 → 只告警，P7 接入官方 API 后解锁。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import metrics, snapshots
from .metrics import MetricResult

# ── 动作类别（ADR-7）────────────────────────────────────────────────────────
STANCH = "stanch"
STRUCTURAL = "structural"
ADVISORY = "advisory"

CRIT, WARN, INFO = "crit", "warn", "info"
_SEV_RANK = {CRIT: 0, WARN: 1, INFO: 2}

#: 阈值单一权威定义，按「规则码」分格。规则与优化器都引用这里，避免两套数字打架。
THRESHOLDS: dict[str, Any] = {
    "stock.days_low.days": 14.0,          # 可供天数低于此值告警
    "stock.days_low.crit_days": 7.0,      # 低于此值升为 crit
    "stock.unsellable_spike.pct": 0.20,   # 不可售数量环比涨幅
    "stock.unsellable_spike.min_qty": 5.0,  # 小基数不报，避免 1→2 也告警
    "stock.excess.min_qty": 1.0,
    "ads.budget_changed_externally.min_pct": 0.05,  # 预算变动小于 5% 不报
    "stanch.max_change_pct": 0.15,        # 止血动作幅度封顶（与 guardrails 一致方向）
}

#: 视为「投放受阻」的 serving_status 关键字（大写匹配）。
_BLOCKED_SERVING = ("OUT_OF_BUDGET", "OUT OF BUDGET")

#: 健康度字段中视为异常的取值。实测该字段可能为空串——空串一律不报。
_UNHEALTHY = ("EXCESS", "LOW_INVENTORY", "AT_RISK", "UNHEALTHY")


@dataclass
class Finding:
    code: str
    layer: str
    severity: str
    action_class: str
    sid: Any
    scope: str
    target_id: str
    target_name: str
    message: str
    metric: str = ""
    current: float = 0.0
    baseline: float = 0.0
    window: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    provenance: str = ""
    intent: Optional[dict[str, Any]] = None

    @property
    def executable(self) -> bool:
        return self.intent is not None

    def line(self) -> str:
        tag = {CRIT: "[紧急]", WARN: "[注意]", INFO: "[提示]"}.get(self.severity, "")
        exe = " ⚙可执行" if self.executable else ""
        return f"{tag} {self.message}{exe}"


@dataclass
class CheckResult:
    """一次巡检的完整结果。

    ``gaps`` 是刻意存在的：某条规则因为缺数据没跑，必须让人看见，
    而不是静默消失（否则用户以为"没告警=没问题"）。
    """
    sid: Any
    layer: str
    findings: list[Finding] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (_SEV_RANK.get(f.severity, 9), f.code))


# ── 工具 ────────────────────────────────────────────────────────────────────
def _pct_change(new: float, old: float) -> float:
    if not old:
        return 0.0
    return (new - old) / abs(old)


def _own_write_recently(target_id: str, within_seconds: float = 7200.0) -> bool:
    """该目标近期是否被 agent 自己写过——用于区分「外部改动」与「自己的操作」。

    审计记录的时间戳是本地可读串（``audit.record`` 用 ``%Y-%m-%d %H:%M:%S``）。
    解析失败时**保守返回 True**（当作是自己改的，不报警），宁可漏报也不让用户
    被自己的操作反复打扰。
    """
    from . import audit
    if not target_id:
        return False
    cutoff = time.time() - within_seconds
    for entry in reversed(audit.load_all()):
        ids = {str(entry.get("target_id") or ""), str(entry.get("search_term") or "")}
        fields = entry.get("fields") or {}
        ids.add(str(fields.get("target_id") or ""))
        for tid in list(entry.get("target_ids") or []):
            ids.add(str(tid))
        if str(target_id) not in ids:
            continue
        raw = str(entry.get("ts") or "")
        try:
            ts = time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            return True
        if ts >= cutoff:
            return True
    return False


def _prov(result: MetricResult) -> str:
    return result.provenance.describe() if result.provenance else ""


# ── L1 规则 ─────────────────────────────────────────────────────────────────
def _rule_stock(res: CheckResult, inv: MetricResult) -> None:
    """库存类 5 条规则。

    ⚠️ 实测教训：领星 FBA 库存接口把 **FBM 商品也一并返回**（某账号 876/876 行为 FBM，
    库存全为 0）。若不按 channel 过滤，"可售为 0"会对全部自发货商品误报。
    """
    rows = [r for r in inv.rows if r.get("channel") == "FBA"]
    if not rows:
        res.skipped.append(
            f"库存规则跳过：本店 {len(inv.rows)} 行库存全部不是 FBA 渠道"
            f"（{'/'.join(sorted({str(r.get('channel') or '?') for r in inv.rows})) or '无数据'}），"
            "FBA 库存规则对 FBM 商品不适用")
        return

    prov = _prov(inv)
    diff = snapshots.diff(res.sid, "inventory", rows, "msku",
                          ["unsellable", "fulfillable"], track_membership=False)
    prev_unsellable = {}
    if diff.has_baseline:
        prev_unsellable = {c.entity_id: c.before for c in diff.changes
                           if c.field == "unsellable"}

    for r in rows:
        msku = r.get("msku") or ""
        name = r.get("product_name") or r.get("asin") or msku
        fulfillable = float(r.get("fulfillable") or 0)
        inbound = float(r.get("inbound_shipped") or 0)
        working = float(r.get("inbound_working") or 0)
        receiving = float(r.get("inbound_receiving") or 0)
        dos = float(r.get("days_of_supply") or 0)
        unsellable = float(r.get("unsellable") or 0)
        excess = float(r.get("excess_qty") or 0)
        health = str(r.get("health_status") or "").upper()

        # 1. 断货：可售为 0 且没有任何在途
        if fulfillable <= 0 and (inbound + working + receiving) <= 0:
            res.findings.append(Finding(
                code="stock.oos", layer="L1", severity=CRIT, action_class=ADVISORY,
                sid=res.sid, scope="msku", target_id=msku, target_name=name,
                metric="fulfillable", current=fulfillable, baseline=0.0,
                message=f"「{name}」({msku}) 已断货：可售 0，无在途库存",
                evidence={k: r.get(k) for k in
                          ("fulfillable", "inbound_shipped", "inbound_working",
                           "inbound_receiving", "reserved", "days_of_supply")},
                provenance=prov))
        # 2. 可供天数不足
        elif dos > 0 and dos < THRESHOLDS["stock.days_low.days"]:
            sev = CRIT if dos < THRESHOLDS["stock.days_low.crit_days"] else WARN
            res.findings.append(Finding(
                code="stock.days_low", layer="L1", severity=sev, action_class=ADVISORY,
                sid=res.sid, scope="msku", target_id=msku, target_name=name,
                metric="days_of_supply", current=dos,
                baseline=THRESHOLDS["stock.days_low.days"],
                message=f"「{name}」({msku}) 可供 {dos:.1f} 天，低于 "
                        f"{THRESHOLDS['stock.days_low.days']:.0f} 天阈值",
                evidence={k: r.get(k) for k in
                          ("fulfillable", "inbound_shipped", "days_of_supply", "sell_through")},
                provenance=prov))

        # 3. 不可售激增（需基线）
        if msku in prev_unsellable:
            before = float(prev_unsellable[msku] or 0)
            if (unsellable >= THRESHOLDS["stock.unsellable_spike.min_qty"]
                    and _pct_change(unsellable, before) >= THRESHOLDS["stock.unsellable_spike.pct"]):
                res.findings.append(Finding(
                    code="stock.unsellable_spike", layer="L1", severity=WARN,
                    action_class=ADVISORY, sid=res.sid, scope="msku",
                    target_id=msku, target_name=name, metric="unsellable",
                    current=unsellable, baseline=before,
                    message=f"「{name}」({msku}) 不可售库存 {before:.0f} → {unsellable:.0f}"
                            f"（{_pct_change(unsellable, before):+.0%}），建议查退货原因",
                    evidence={"unsellable_before": before, "unsellable_now": unsellable},
                    provenance=prov))

        # 4. 库存健康度异常（空值不报——实测该字段可能为空串）
        if health and any(bad in health for bad in _UNHEALTHY):
            res.findings.append(Finding(
                code="stock.health_bad", layer="L1", severity=INFO, action_class=ADVISORY,
                sid=res.sid, scope="msku", target_id=msku, target_name=name,
                metric="health_status", message=f"「{name}」({msku}) 库存健康度：{health}",
                evidence={"health_status": health, "days_of_supply": dos},
                provenance=prov))

        # 5. 冗余库存
        if excess >= THRESHOLDS["stock.excess.min_qty"]:
            res.findings.append(Finding(
                code="stock.excess", layer="L1", severity=INFO, action_class=ADVISORY,
                sid=res.sid, scope="msku", target_id=msku, target_name=name,
                metric="excess_qty", current=excess,
                message=f"「{name}」({msku}) 预估冗余库存 {excess:.0f} 件，长期仓储费风险",
                evidence={"excess_qty": excess, "age_365_plus": r.get("age_365_plus"),
                          "sell_through": r.get("sell_through")},
                provenance=prov))

    if not diff.has_baseline:
        res.skipped.append("不可售激增规则跳过：本次为首轮，正在建立库存基线（冷启动保护）")
    snapshots.save(res.sid, "inventory", rows, "msku")


def _rule_campaign(res: CheckResult, camp: MetricResult) -> None:
    """广告活动配置类 3 条规则（预算耗尽 / 非预期暂停 / 外部改动）。"""
    rows = camp.rows
    if not rows:
        res.skipped.append("广告活动规则跳过：本店无广告活动数据")
        return
    prov = _prov(camp)
    by_id = {str(r.get("campaign_id")): r for r in rows}

    # 预算耗尽：快照即可判，不需要基线
    for r in rows:
        serving = str(r.get("serving_status") or "").upper()
        if any(k in serving for k in _BLOCKED_SERVING):
            budget = float(r.get("daily_budget") or 0)
            new_budget = round(budget * (1 + THRESHOLDS["stanch.max_change_pct"]), 2)
            res.findings.append(Finding(
                code="ads.campaign_out_of_budget", layer="L1", severity=WARN,
                action_class=STANCH, sid=res.sid, scope="campaign",
                target_id=str(r.get("campaign_id")), target_name=str(r.get("name") or ""),
                metric="daily_budget", current=budget, baseline=new_budget,
                message=f"活动「{r.get('name')}」预算耗尽停投（{serving}），"
                        f"建议日预算 {budget:.2f} → {new_budget:.2f}",
                evidence={"serving_status": serving, "daily_budget": budget,
                          "state": r.get("state")},
                provenance=prov,
                intent={"op_type": "campaign_budget", "sid": res.sid,
                        "target_id": r.get("campaign_id"),
                        "target_name": r.get("name"),
                        "change": {"daily_budget": new_budget},
                        "before": {"daily_budget": budget}}))

    diff = snapshots.diff(res.sid, "campaigns", rows, "campaign_id",
                          ["state", "daily_budget", "serving_status"],
                          track_membership=False)
    if not diff.has_baseline:
        res.skipped.append("活动状态/预算变更规则跳过：本次为首轮，正在建立基线（冷启动保护）")
        snapshots.save(res.sid, "campaigns", rows, "campaign_id")
        return

    for c in diff.changes:
        if c.change != "changed":
            continue
        row = by_id.get(c.entity_id) or {}
        name = str(row.get("name") or c.entity_id)

        if c.field == "state" and str(c.after).lower() == "paused" \
                and str(c.before).lower() == "enabled":
            if _own_write_recently(c.entity_id):
                continue
            res.findings.append(Finding(
                code="ads.campaign_unexpected_pause", layer="L1", severity=CRIT,
                action_class=ADVISORY, sid=res.sid, scope="campaign",
                target_id=c.entity_id, target_name=name, metric="state",
                message=f"活动「{name}」被暂停，且非本 agent 操作——请检查账号或合规状态",
                evidence={"state_before": c.before, "state_after": c.after,
                          "serving_status": row.get("serving_status")},
                provenance=prov))

        elif c.field == "daily_budget":
            before, after = float(c.before or 0), float(c.after or 0)
            if abs(_pct_change(after, before)) < THRESHOLDS["ads.budget_changed_externally.min_pct"]:
                continue
            if _own_write_recently(c.entity_id):
                continue
            res.findings.append(Finding(
                code="ads.budget_changed_externally", layer="L1", severity=WARN,
                action_class=ADVISORY, sid=res.sid, scope="campaign",
                target_id=c.entity_id, target_name=name, metric="daily_budget",
                current=after, baseline=before,
                message=f"活动「{name}」日预算被外部改动：{before:.2f} → {after:.2f}"
                        f"（{_pct_change(after, before):+.0%}）",
                evidence={"budget_before": before, "budget_after": after},
                provenance=prov))

    snapshots.save(res.sid, "campaigns", rows, "campaign_id")


# ── 入口 ────────────────────────────────────────────────────────────────────
def check_l1(sid: Any) -> CheckResult:
    """L1 快照层巡检。当前走领星轮询；接入推送源后同一批规则自动升级（ADR-8/9）。"""
    from . import datasources
    datasources.install_defaults()

    res = CheckResult(sid=sid, layer="L1")

    inv = metrics.get_metric(metrics.INVENTORY_FBA.key, {"sid": sid})
    if inv.ok:
        res.provenance.append(f"{metrics.INVENTORY_FBA.key}：{_prov(inv)}")
        _rule_stock(res, inv)
    else:
        res.gaps.append(inv.gap.describe())

    camp = metrics.get_metric(metrics.ADS_CAMPAIGN_CONFIG.key, {"sid": sid})
    if camp.ok:
        res.provenance.append(f"{metrics.ADS_CAMPAIGN_CONFIG.key}：{_prov(camp)}")
        _rule_campaign(res, camp)
    else:
        res.gaps.append(camp.gap.describe())

    return res


def render(result: CheckResult) -> str:
    """纯文本渲染（CLI / webhook 降级 / 日志用）。飞书卡片渲染在 P2 另做。"""
    lines = [f"== 店铺巡检 {result.layer} · sid {result.sid} =="]
    findings = result.sorted_findings()
    if findings:
        counts: dict[str, int] = {}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        summary = " ".join(f"{k}={v}" for k, v in sorted(
            counts.items(), key=lambda kv: _SEV_RANK.get(kv[0], 9)))
        lines.append(f"发现 {len(findings)} 条（{summary}）")
        for f in findings:
            lines.append("  " + f.line())
    else:
        lines.append("未发现异常。")
    for s in result.skipped:
        lines.append(f"  · 跳过：{s}")
    for g in result.gaps:
        lines.append(f"  ! 数据缺口：{g}")
    for p in result.provenance:
        lines.append(f"  数据来源：{p}")
    return "\n".join(lines) + "\n"
