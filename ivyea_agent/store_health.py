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
from dataclasses import dataclass, field, replace
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
    # —— L3 隔日层 ——
    "ads.acos_breach.factor": 1.5,        # ACOS 超过目标的倍数
    "ads.acos_breach.min_spend": 200.0,   # 花费门槛，小花费不值得惊动
    "ads.cpc_jump.pct": 0.30,             # CPC 环比涨幅
    "ads.cpc_jump.min_clicks": 30.0,      # 点击太少时 CPC 波动无意义
    "ads.budget_capped.ratio": 0.95,      # 日均花费 / 日预算 达到此比例视为打满
    "sales.drop.ratio": 0.5,              # 销售额跌破基线的比例
    "sales.drop.min_baseline": 100.0,     # 基线太小不报（新品/长尾噪音）
    "profit.margin_erosion.pp": 0.05,     # 毛利率下降的百分点
    # —— L3 · Listing 维度（来自 listing.snapshot 的多窗口销量/广告）——
    # 这一组只依赖「指标契约」，不依赖是哪个 provider 给的数：
    # 领星 MCP 现在能给，换成 SP-API / Ads API 报表后规则一行不用改（ADR-8）。
    "sales.listing_drop.factor": 0.5,     # 近 7 日日均 / 近 30 日日均 跌破此比例
    "sales.listing_drop.min_avg30": 0.5,  # 30 日日均低于此值不报（长尾噪音）
    "sales.listing_stall.min_volume_30": 5.0,  # 30 日总销量门槛，太小不算"断流"
    "ads.listing_acos.min_spend": 100.0,  # listing 级广告花费门槛
    "ads.listing_no_sales.min_spend": 50.0,   # 有花费零销售额的门槛
    # —— L2 日内层 ——
    "ads.spend_burst.factor": 2.5,        # 小时花费速率 / 基线速率
    "ads.spend_burst.min_spend": 20.0,    # 本段增量花费门槛
    "ads.spend_burst.order_tolerance": 1.5,  # 订单同步增长到此倍数即视为「花得值」
    "ads.impression_zero.min_yesterday": 1000.0,
    "ads.click_no_order.factor": 2.0,     # 当日点击 / 历史日均点击
    "ads.click_no_order.min_clicks": 20.0,
    "l2.min_gap_minutes": 20.0,           # 采样间隔下限，太短则增量全是噪声
    "l2.baseline_min_days": 3,            # 同时段基线所需的最少历史天数
    # —— Listing 快照（领星 MCP 源）——
    "listing.rating_low.stars": 3.5,      # 评分低于此值告警
    "listing.rating_low.min_reviews": 3.0,  # 评价太少时评分不稳定，不报
    "review.rating_drop.delta": 0.3,      # 评分下降幅度（星）
    "rank.drop.pct": 0.30,                # 排名恶化比例（rank 数值越大越差）
    "price.changed.pct": 0.05,            # 价格变动幅度
    "stock.fbm_low.days": 14.0,           # FBM 可供天数下限
    "buybox.crowded.sellers": 3.0,        # 跟卖卖家数达到此值视为拥挤
    "variants.collapse.min": 3.0,         # 同母体 ASIN 的同类告警达到此数量则合并成一条
}

def threshold(key: str) -> Any:
    """读阈值。settings 里的 ``store_thresholds`` 覆盖默认值。

    做成函数而不是直接读常量，是为了让阈值能在飞书里当场调（方案 P6），
    调完立刻生效、不用改代码重启。未知 key 直接抛错，避免手滑写错键名后
    静默用了默认值还以为改成功了。
    """
    if key not in THRESHOLDS:
        raise KeyError(f"未知阈值：{key}")
    from . import config
    override = (config.load_settings().get("store_thresholds") or {}).get(key)
    return THRESHOLDS[key] if override is None else override


def set_threshold(key: str, value: Any) -> Any:
    """改阈值并落盘。返回生效值。"""
    if key not in THRESHOLDS:
        raise KeyError(f"未知阈值：{key}")
    from . import config
    default = THRESHOLDS[key]
    try:
        value = type(default)(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 需要 {type(default).__name__} 类型") from exc
    settings = config.load_settings()
    store = dict(settings.get("store_thresholds") or {})
    store[key] = value
    settings["store_thresholds"] = store
    config.save_settings(settings)
    return value


def reset_threshold(key: str = "") -> int:
    """清除覆盖，回到默认。不传 key 则全部清除。返回清掉的条数。"""
    from . import config
    settings = config.load_settings()
    store = dict(settings.get("store_thresholds") or {})
    n = len(store) if not key else (1 if store.pop(key, None) is not None else 0)
    if not key:
        store = {}
    settings["store_thresholds"] = store
    config.save_settings(settings)
    return n


def threshold_table() -> list[dict[str, Any]]:
    """当前所有阈值 + 是否被覆盖。飞书里 /threshold 不带参数时展示。"""
    from . import config
    store = config.load_settings().get("store_thresholds") or {}
    return [{"key": k, "default": v, "current": store.get(k, v),
             "overridden": k in store} for k, v in sorted(THRESHOLDS.items())]


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
    #: 同一件事的分组键（listing 类规则填母体 ASIN）。见 ``collapse_variants``。
    group_id: str = ""
    #: 合并后代表的变体数量；1 表示没被合并过。
    group_size: int = 1

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
    #: 出现缺口的**指标 key** → 原因。``gaps`` 是给人看的句子，这里是给代码用的。
    #: 多店铺巡检要按指标维度判断"是这个店不支持，还是这个指标全线挂了"，
    #: 靠正则从中文句子里抠 metric key 是自找麻烦。
    gap_metrics: dict[str, str] = field(default_factory=dict)

    def add_gap(self, result: "MetricResult") -> None:
        """记一次取数缺口：人话进 ``gaps``，指标 key 进 ``gap_metrics``。"""
        if result.gap is None:
            return
        self.gaps.append(result.gap.describe())
        self.gap_metrics[result.gap.metric] = result.gap.reason

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (_SEV_RANK.get(f.severity, 9), f.code))


def collapse_variants(findings: list[Finding]) -> list[Finding]:
    """把同一母体 ASIN 下的同类告警合并成一条。

    为什么需要：实测日本站一个母体 ASIN 挂着 30 个变体（同款不同尺寸/颜色），
    评分都是 3.0 星。不合并的话，"这个款评分低"这一件事会被报成 24 条，
    早报卡片直接被一个款刷满，别的店的问题就看不见了。

    两条硬规则：
    - **带 intent 的绝不合并**。那是会真去改钱的动作，一条 intent 对一个目标，
      合并等于把 24 个写操作缩成 1 个按钮，点下去谁也说不清改了哪个。
    - **不够 ``variants.collapse.min`` 条的不合并**。两三条变体照常逐条列出，
      合并只用来治"一个款刷屏"，不是用来藏信息。
    """
    limit = int(threshold("variants.collapse.min"))
    groups: dict[tuple[str, str], list[Finding]] = {}
    passthrough: list[Finding] = []
    for f in findings:
        if f.intent is not None or not f.group_id:
            passthrough.append(f)
            continue
        groups.setdefault((f.code, f.group_id), []).append(f)

    out: list[Finding] = list(passthrough)
    for (_code, group_id), items in groups.items():
        if len(items) < limit:
            out.extend(items)
            continue
        head = items[0]
        worst = min(items, key=lambda x: _SEV_RANK.get(x.severity, 9))
        merged = replace(
            head,
            severity=worst.severity,
            target_id=group_id,
            group_size=len(items),
            message=f"{head.message}（同母体 {group_id} 下另有 {len(items) - 1} "
                    f"个变体同样如此）",
            evidence={**head.evidence, "variant_count": len(items),
                      "parent_asin": group_id,
                      "variants": [x.target_id for x in items[:10]]},
        )
        out.append(merged)
    return out


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
        elif dos > 0 and dos < threshold("stock.days_low.days"):
            sev = CRIT if dos < threshold("stock.days_low.crit_days") else WARN
            res.findings.append(Finding(
                code="stock.days_low", layer="L1", severity=sev, action_class=ADVISORY,
                sid=res.sid, scope="msku", target_id=msku, target_name=name,
                metric="days_of_supply", current=dos,
                baseline=threshold("stock.days_low.days"),
                message=f"「{name}」({msku}) 可供 {dos:.1f} 天，低于 "
                        f"{THRESHOLDS['stock.days_low.days']:.0f} 天阈值",
                evidence={k: r.get(k) for k in
                          ("fulfillable", "inbound_shipped", "days_of_supply", "sell_through")},
                provenance=prov))

        # 3. 不可售激增（需基线）
        if msku in prev_unsellable:
            before = float(prev_unsellable[msku] or 0)
            if (unsellable >= threshold("stock.unsellable_spike.min_qty")
                    and _pct_change(unsellable, before) >= threshold("stock.unsellable_spike.pct")):
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
        if excess >= threshold("stock.excess.min_qty"):
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
            new_budget = round(budget * (1 + threshold("stanch.max_change_pct")), 2)
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
            if abs(_pct_change(after, before)) < threshold("ads.budget_changed_externally.min_pct"):
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
#: 未开通广告的店铺跳过这些指标时给出的说法。
ADS_NOT_ENABLED = "该店铺未在领星开通广告，广告类规则不适用"


def ads_enabled(sid: Any) -> bool:
    """该店是否开通广告。清单不可用时按"开通"处理，理由见 ``stores.supports_ads``。"""
    from . import stores
    return stores.supports_ads(sid)


def check_l1(sid: Any) -> CheckResult:
    """L1 快照层巡检。当前走领星轮询；接入推送源后同一批规则自动升级（ADR-8/9）。"""
    from . import datasources
    datasources.install_defaults()

    res = CheckResult(sid=sid, layer="L1")
    has_ads = ads_enabled(sid)

    inv = metrics.get_metric(metrics.INVENTORY_FBA.key, {"sid": sid})
    if inv.ok:
        res.provenance.append(f"{metrics.INVENTORY_FBA.key}：{_prov(inv)}")
        _rule_stock(res, inv)
    else:
        res.add_gap(inv)

    # 广告规则先问"这个店有没有广告"再取数。未开通的店调领星广告接口稳定返回
    # code=102，那不是故障而是能力边界：记 skipped 不记 gap，否则连续失败计数器
    # 会对这些店永远告警（且永远不会恢复）。顺带省掉一次注定失败的调用。
    if not has_ads:
        res.skipped.append(f"广告活动规则跳过：{ADS_NOT_ENABLED}")
    else:
        camp = metrics.get_metric(metrics.ADS_CAMPAIGN_CONFIG.key, {"sid": sid})
        if camp.ok:
            res.provenance.append(f"{metrics.ADS_CAMPAIGN_CONFIG.key}：{_prov(camp)}")
            _rule_campaign(res, camp)
        else:
            res.add_gap(camp)

    snap = metrics.get_metric(metrics.LISTING_SNAPSHOT.key, {"sid": sid})
    if snap.ok:
        res.provenance.append(f"{metrics.LISTING_SNAPSHOT.key}：{_prov(snap)}")
        _rule_listing(res, snap)
    else:
        res.add_gap(snap)

    follow = metrics.get_metric(metrics.FOLLOW_SALE.key, {"sid": sid})
    if follow.ok:
        res.provenance.append(f"{metrics.FOLLOW_SALE.key}：{_prov(follow)}")
        _rule_follow_sale(res, follow)
    else:
        res.add_gap(follow)

    # 一个款 30 个变体会把同一件事报 24 遍，合并后才看得见别的店的问题
    res.findings = collapse_variants(res.findings)
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


# ── L3 隔日层 ───────────────────────────────────────────────────────────────
def _window_days(days: int, exclude_recent: int = 1) -> tuple[list[str], list[str]]:
    """返回 (近期窗口, 对照窗口)，均为升序日期串。

    ``exclude_recent=1`` 是因为报表 T+1：今天拉不到今天的完整数据，
    把最近 1 天排除掉，避免拿半天数据和整天基线比，得出"销量腰斩"的假告警。
    """
    import datetime
    today = datetime.date.today()
    def _span(offset: int) -> list[str]:
        return sorted((today - datetime.timedelta(days=d)).isoformat()
                      for d in range(offset, offset + days))
    return _span(exclude_recent), _span(exclude_recent + days)


def _agg_rows(rows: list[dict[str, Any]], key: str,
              dates: set[str]) -> dict[str, dict[str, float]]:
    """按实体聚合指定日期的报表行。"""
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.get("date") not in dates:
            continue
        k = str(r.get(key) or "")
        if not k:
            continue
        b = out.setdefault(k, {"impressions": 0.0, "clicks": 0.0, "spend": 0.0,
                               "orders": 0.0, "sales": 0.0, "days": 0.0})
        for f in ("impressions", "clicks", "spend", "orders", "sales"):
            b[f] += float(r.get(f) or 0)
        b["days"] += 1
    return out


def _derive(b: dict[str, float]) -> dict[str, float]:
    clicks, spend, sales = b["clicks"], b["spend"], b["sales"]
    return {
        "acos": (spend / sales) if sales else 0.0,
        "cpc": (spend / clicks) if clicks else 0.0,
        "cvr": (b["orders"] / clicks) if clicks else 0.0,
        "has_sales": bool(sales),
    }


def _rule_ads_l3(res: CheckResult, rep: MetricResult, cfg: MetricResult,
                 recent: list[str], base: list[str], target_acos: float,
                 acos_note: str) -> None:
    rows = rep.rows
    if not rows:
        res.skipped.append("广告日报表规则跳过：窗口内无报表数据")
        return
    prov = _prov(rep)
    cur = _agg_rows(rows, "campaign_id", set(recent))
    old = _agg_rows(rows, "campaign_id", set(base))
    budgets = {str(r.get("campaign_id")): r for r in cfg.rows} if cfg.ok else {}
    if not budgets:
        res.skipped.append("预算打满规则跳过：未取到活动配置（拿不到日预算做分母）")

    breach_factor = threshold("ads.acos_breach.factor")
    for cid, b in cur.items():
        name = str((budgets.get(cid) or {}).get("name") or cid)
        d = _derive(b)
        spend = b["spend"]

        # 1. ACOS 超标
        if spend >= threshold("ads.acos_breach.min_spend") and d["has_sales"] \
                and d["acos"] > target_acos * breach_factor:
            budget = float((budgets.get(cid) or {}).get("daily_budget") or 0)
            intent = None
            if budget > 0:
                new_budget = round(budget * (1 - threshold("stanch.max_change_pct")), 2)
                intent = {"op_type": "campaign_budget", "sid": res.sid, "target_id": cid,
                          "target_name": name,
                          "change": {"daily_budget": new_budget},
                          "before": {"daily_budget": budget}}
            res.findings.append(Finding(
                code="ads.acos_breach", layer="L3", severity=WARN, action_class=STANCH,
                sid=res.sid, scope="campaign", target_id=cid, target_name=name,
                metric="acos", current=d["acos"], baseline=target_acos,
                window=f"{recent[0]}~{recent[-1]}",
                message=f"活动「{name}」ACOS {d['acos']:.0%}，超目标 {target_acos:.0%} 的 "
                        f"{breach_factor:g} 倍（花费 {spend:.2f}）",
                evidence={"spend": spend, "sales": b["sales"], "orders": b["orders"],
                          "acos": round(d["acos"], 4), "target_acos": round(target_acos, 4),
                          "target_note": acos_note},
                provenance=prov, intent=intent))

        # 2. CPC 跳涨且转化未改善 —— 真正的杠杆在关键词 bid，交给优化器，这里只报
        ob = old.get(cid)
        if ob and b["clicks"] >= threshold("ads.cpc_jump.min_clicks"):
            od = _derive(ob)
            if od["cpc"] > 0 and _pct_change(d["cpc"], od["cpc"]) >= threshold("ads.cpc_jump.pct") \
                    and d["cvr"] <= od["cvr"]:
                res.findings.append(Finding(
                    code="ads.cpc_jump", layer="L3", severity=WARN, action_class=ADVISORY,
                    sid=res.sid, scope="campaign", target_id=cid, target_name=name,
                    metric="cpc", current=d["cpc"], baseline=od["cpc"],
                    window=f"{recent[0]}~{recent[-1]} vs {base[0]}~{base[-1]}",
                    message=f"活动「{name}」CPC {od['cpc']:.2f} → {d['cpc']:.2f}"
                            f"（{_pct_change(d['cpc'], od['cpc']):+.0%}）且转化未改善",
                    evidence={"cpc_now": round(d["cpc"], 4), "cpc_before": round(od["cpc"], 4),
                              "cvr_now": round(d["cvr"], 4), "cvr_before": round(od["cvr"], 4),
                              "clicks": b["clicks"]},
                    provenance=prov))

        # 3. 预算打满且表现健康 → 提预算（止血的反向：别卡住好活动）
        conf = budgets.get(cid)
        if conf and b["days"]:
            budget = float(conf.get("daily_budget") or 0)
            daily_spend = spend / b["days"]
            if budget > 0 and daily_spend >= budget * threshold("ads.budget_capped.ratio") \
                    and d["has_sales"] and d["acos"] < target_acos:
                new_budget = round(budget * (1 + threshold("stanch.max_change_pct")), 2)
                res.findings.append(Finding(
                    code="ads.budget_capped", layer="L3", severity=INFO, action_class=STANCH,
                    sid=res.sid, scope="campaign", target_id=cid, target_name=name,
                    metric="daily_budget", current=daily_spend, baseline=budget,
                    window=f"{recent[0]}~{recent[-1]}",
                    message=f"活动「{name}」日均花费 {daily_spend:.2f} 已打满预算 {budget:.2f}，"
                            f"而 ACOS {d['acos']:.0%} 优于目标，建议提到 {new_budget:.2f}",
                    evidence={"daily_spend": round(daily_spend, 2), "daily_budget": budget,
                              "acos": round(d["acos"], 4), "target_acos": round(target_acos, 4)},
                    provenance=prov,
                    intent={"op_type": "campaign_budget", "sid": res.sid, "target_id": cid,
                            "target_name": name,
                            "change": {"daily_budget": new_budget},
                            "before": {"daily_budget": budget}}))


def _rule_profit_l3(res: CheckResult, cur: MetricResult, old: MetricResult,
                    recent: list[str], base: list[str]) -> None:
    if not cur.ok or not cur.rows:
        res.skipped.append("销量/毛利规则跳过：窗口内无 ASIN 利润数据")
        return
    prov = _prov(cur)
    prev = {str(r.get("asin")): r for r in (old.rows if old.ok else [])}
    if not prev:
        res.skipped.append("销量断崖规则跳过：无对照期利润数据")

    for r in cur.rows:
        asin = str(r.get("asin") or "")
        if not asin:
            continue
        p = prev.get(asin)
        if not p:
            continue
        sales, base_sales = float(r.get("sales_amount") or 0), float(p.get("sales_amount") or 0)
        if base_sales >= threshold("sales.drop.min_baseline") \
                and sales < base_sales * threshold("sales.drop.ratio"):
            res.findings.append(Finding(
                code="sales.drop", layer="L3", severity=CRIT, action_class=ADVISORY,
                sid=res.sid, scope="asin", target_id=asin, target_name=asin,
                metric="sales_amount", current=sales, baseline=base_sales,
                window=f"{recent[0]}~{recent[-1]} vs {base[0]}~{base[-1]}",
                message=f"ASIN {asin} 销售额 {base_sales:.2f} → {sales:.2f}"
                        f"（{_pct_change(sales, base_sales):+.0%}）",
                evidence={"sales_now": sales, "sales_before": base_sales,
                          "ads_cost": r.get("ads_cost")},
                provenance=prov))

        rate, base_rate = float(r.get("gross_rate") or 0), float(p.get("gross_rate") or 0)
        # 领星毛利率可能以百分数返回（如 23.5 表示 23.5%），统一折算成小数再比
        if rate > 1 or base_rate > 1:
            rate, base_rate = rate / 100.0, base_rate / 100.0
        if base_rate and (base_rate - rate) >= threshold("profit.margin_erosion.pp"):
            res.findings.append(Finding(
                code="profit.margin_erosion", layer="L3", severity=WARN, action_class=ADVISORY,
                sid=res.sid, scope="asin", target_id=asin, target_name=asin,
                metric="gross_rate", current=rate, baseline=base_rate,
                window=f"{recent[0]}~{recent[-1]} vs {base[0]}~{base[-1]}",
                message=f"ASIN {asin} 毛利率 {base_rate:.1%} → {rate:.1%}"
                        f"（下降 {(base_rate - rate) * 100:.1f} 个百分点）",
                evidence={"gross_rate_now": round(rate, 4),
                          "gross_rate_before": round(base_rate, 4),
                          "gross_profit": r.get("gross_profit")},
                provenance=prov))


def _rule_optimizer(res: CheckResult, sid: Any) -> None:
    """可执行的结构型/止血型动作委托给优化器。

    刻意**不重造**否词/收割/调价逻辑：优化器已内建冷却期、历史否决记忆、
    毛利率推目标、护栏拦截。重写一套必然与它漂移，出现「巡检说该否、优化器说冷却中」。
    """
    from . import lingxing_optimizer, lingxing_write

    try:
        out = lingxing_optimizer.run_store(int(sid))
    except Exception as exc:                       # noqa: BLE001
        res.gaps.append(f"优化器候选不可用：{exc}")
        return

    blocked = 0
    for cand in out.get("candidates", []):
        lever = str(cand.get("lever") or "")
        if lever == "错误":
            res.gaps.append(f"优化器数据源报错：{cand.get('block_reason') or cand.get('rationale')}")
            continue
        if cand.get("blocked"):
            blocked += 1
            continue
        intent = lingxing_write.candidate_to_intent(cand)
        op = str(cand.get("op_type") or "")
        # 否词/加词不可逆 → 结构型，需统计显著性（优化器已保证）；调价可逆 → 止血型
        action_class = STRUCTURAL if op in ("negate_keyword", "add_keyword") else STANCH
        res.findings.append(Finding(
            code=f"ads.opt.{op or lever}", layer="L3", severity=WARN,
            action_class=action_class, sid=sid, scope="keyword",
            target_id=str(cand.get("target_id") or cand.get("target_name") or ""),
            target_name=str(cand.get("target_name") or ""),
            metric="", message=f"[{lever}] {cand.get('rule') or cand.get('rationale')}",
            evidence={"metrics": cand.get("metrics"),
                      "significance": cand.get("significance"),
                      "rationale": cand.get("rationale"),
                      "target_acos": cand.get("opt_target")},
            provenance=f"优化器窗口 {out.get('window_days')} 天 · {out.get('note', '')}",
            intent=intent))
    if blocked:
        res.skipped.append(f"优化器候选有 {blocked} 条被护栏/冷却/历史否决拦截，未纳入建议")


def _rule_listing_sales_l3(res: CheckResult, snap: MetricResult,
                           target_acos: float, acos_note: str) -> None:
    """Listing 维度的销量与广告效率（L3）。

    **为什么这一层要有 listing 维度**：活动级报表看不到"哪个货卖不动了"——
    一个活动下挂十个 ASIN，其中一个断流，活动整体的 ACOS 可能还很好看。
    出问题的是货，不是活动。

    这批规则只读**指标契约**里的多窗口销量字段（`volume_7` / `volume_30` /
    `amount_7` / `spend_7`），不关心是谁给的数：现在是领星 MCP 的 erp_listing，
    换成 SP-API / Ads API 报表后规则一行不用改（ADR-8 分层的意义就在这）。

    **全部只告警、不带 intent**：一条 listing 对应哪个广告活动，快照里没有这个
    映射关系。凭 ASIN 猜一个活动去改预算，改错的是别人的钱。要动手得先有
    "listing → campaign" 的确定映射，那是 Ads API 才给得起的东西。
    """
    if not snap.ok:
        # 没有任何数据源提供 listing 快照（例如只配了领星 OpenAPI、没配 MCP）
        # 是**能力边界**，不是故障：记 skipped，不喂连续失败告警。
        # 反过来，源存在却取不到数才是缺口——那意味着规则本该跑却没跑。
        if "没有任何已注册数据源支持该指标" in str(getattr(snap.gap, "reason", "")):
            res.skipped.append("Listing 销量规则跳过：当前数据源不提供 Listing 快照"
                               "（配上领星 MCP 或亚马逊 SP-API 后自动生效）")
        else:
            res.add_gap(snap)
        return
    rows = snap.rows
    if not rows:
        res.skipped.append("Listing 销量规则跳过：无 Listing 快照数据")
        return
    res.provenance.append(f"listing.snapshot：{_prov(snap)}")

    # 全店 7/30 日销量都为 0 → 不是"全线断流"，是这个账号根本没有销量数据。
    # 逐条报出来会刷屏，而且每一条都是假的。这种整体性缺失要当**数据缺口**报。
    if not any(float(r.get("volume_30") or 0) or float(r.get("volume_7") or 0)
               for r in rows):
        res.gaps.append(
            f"Listing 销量规则跳过：{len(rows)} 条 listing 的 7/30 日销量全为 0，"
            "该数据源未提供销量（不是真的全部断流）")
        res.gap_metrics["listing.snapshot"] = "no_sales_fields"
        return

    prov = _prov(snap)
    drop_factor = threshold("sales.listing_drop.factor")
    min_avg30 = threshold("sales.listing_drop.min_avg30")
    stall_min = threshold("sales.listing_stall.min_volume_30")
    acos_min_spend = threshold("ads.listing_acos.min_spend")
    no_sales_min_spend = threshold("ads.listing_no_sales.min_spend")
    breach_factor = threshold("ads.acos_breach.factor")

    for r in rows:
        if str(r.get("status_text") or "") not in ("在售", "", "Active"):
            continue                     # 已下架的 listing 没销量是应该的
        asin = str(r.get("asin") or r.get("msku") or "")
        name = str(r.get("title") or asin)[:60]
        target = str(r.get("msku") or asin)
        v7 = float(r.get("volume_7") or 0)
        v30 = float(r.get("volume_30") or 0)
        avg7 = float(r.get("avg_volume_7") or 0) or (v7 / 7.0)
        avg30 = float(r.get("avg_volume_30") or 0) or (v30 / 30.0)
        spend7 = float(r.get("spend_7") or 0)
        amount7 = float(r.get("amount_7") or 0)
        # group_id = 母体 ASIN：同一个款的一堆变体同时下滑时，collapse_variants
        # 会把它们并成一条（一个母体 30 个变体能把整张卡占满，实测过）。
        common = {"sid": r.get("sid"), "scope": "listing", "target_id": target,
                  "target_name": name, "provenance": prov,
                  "group_id": str(r.get("parent_asin") or "")}

        # 断流：30 日卖得动、近 7 日一件没有
        if v30 >= stall_min and v7 <= 0:
            res.findings.append(Finding(
                code="sales.listing_stall", layer="L3", severity=CRIT,
                action_class=ADVISORY, metric="volume_7",
                current=0.0, baseline=v30, window="近 7 日 vs 近 30 日",
                message=f"「{name}」近 7 日 0 销量（近 30 日 {v30:.0f} 件），疑似断流",
                evidence={"asin": asin, "volume_7": 0, "volume_30": v30,
                          "status": r.get("status_text"),
                          "fulfillable": r.get("fulfillable"),
                          "quantity": r.get("quantity")},
                **common))
        # 下滑：7 日日均跌破 30 日日均的一定比例
        elif avg30 >= min_avg30 and avg7 < avg30 * drop_factor:
            res.findings.append(Finding(
                code="sales.listing_drop", layer="L3", severity=WARN,
                action_class=ADVISORY, metric="avg_volume_7",
                current=avg7, baseline=avg30, window="近 7 日均 vs 近 30 日均",
                message=f"「{name}」日均销量 {avg7:.1f} 件，跌到 30 日均 "
                        f"{avg30:.1f} 件的 {(avg7 / avg30):.0%}",
                evidence={"asin": asin, "avg_volume_7": round(avg7, 2),
                          "avg_volume_30": round(avg30, 2),
                          "volume_7": v7, "volume_30": v30,
                          "stars": r.get("stars"), "rank": r.get("rank")},
                **common))

        # 广告：有花费、零销售额 —— 纯烧钱，先看到再说
        if spend7 >= no_sales_min_spend and amount7 <= 0:
            res.findings.append(Finding(
                code="ads.listing_spend_no_sales", layer="L3", severity=CRIT,
                action_class=ADVISORY, metric="spend_7",
                current=spend7, baseline=0.0, window="近 7 日",
                message=f"「{name}」近 7 日广告花 {spend7:,.2f}，销售额为 0",
                evidence={"asin": asin, "spend_7": round(spend7, 2), "amount_7": 0,
                          "volume_7": v7},
                **common))
        elif spend7 >= acos_min_spend and amount7 > 0:
            acos = spend7 / amount7
            if acos > target_acos * breach_factor:
                res.findings.append(Finding(
                    code="ads.listing_acos_breach", layer="L3", severity=WARN,
                    action_class=ADVISORY, metric="acos",
                    current=acos, baseline=target_acos, window="近 7 日",
                    message=f"「{name}」listing 级 ACOS {acos:.0%}，"
                            f"超目标 {target_acos:.0%} 的 {breach_factor:g} 倍"
                            f"（{acos_note}）",
                    evidence={"asin": asin, "spend_7": round(spend7, 2),
                              "amount_7": round(amount7, 2),
                              "acos": round(acos, 4),
                              "target_acos": round(target_acos, 4)},
                    **common))


def check_l3(sid: Any, days: int = 7, *, include_optimizer: bool = True) -> CheckResult:
    """L3 隔日层巡检。报表 T+1，所以窗口排除最近 1 天。"""
    from . import datasources, lingxing_optimizer
    datasources.install_defaults()

    res = CheckResult(sid=sid, layer="L3")
    recent, base = _window_days(days)
    has_ads = ads_enabled(sid)

    try:
        target_acos, _brk, _margin, acos_note = lingxing_optimizer.resolve_target_acos(int(sid))
    except Exception as exc:                       # noqa: BLE001
        target_acos, acos_note = 0.30, f"目标ACOS 推导失败（{exc}），暂用 30%"
        res.gaps.append(acos_note)

    if not has_ads:
        res.skipped.append(f"广告日报表规则跳过：{ADS_NOT_ENABLED}")
    else:
        rep = metrics.get_metric(metrics.ADS_CAMPAIGN_REPORT.key, {"sid": sid},
                                metrics.Window(tuple(base + recent)))
        cfg = metrics.get_metric(metrics.ADS_CAMPAIGN_CONFIG.key, {"sid": sid})
        if rep.ok:
            res.provenance.append(f"{metrics.ADS_CAMPAIGN_REPORT.key}：{_prov(rep)}")
            _rule_ads_l3(res, rep, cfg, recent, base, target_acos, acos_note)
        else:
            res.add_gap(rep)

    cur_profit = metrics.get_metric(metrics.PROFIT_ASIN.key, {"sid": sid},
                                    metrics.Window(tuple(recent)))
    old_profit = metrics.get_metric(metrics.PROFIT_ASIN.key, {"sid": sid},
                                    metrics.Window(tuple(base)))
    if cur_profit.ok:
        res.provenance.append(f"{metrics.PROFIT_ASIN.key}：{_prov(cur_profit)}")
        _rule_profit_l3(res, cur_profit, old_profit, recent, base)
    else:
        res.add_gap(cur_profit)

    # Listing 维度：活动级报表看不到"哪个货卖不动了"。这一层不依赖广告开通与否——
    # 断流和销量下滑跟有没有投广告无关，未开通广告的店同样要看。
    snap = metrics.get_metric(metrics.LISTING_SNAPSHOT.key, {"sid": sid})
    _rule_listing_sales_l3(res, snap, target_acos, acos_note)

    # 优化器的四根杠杆全在广告上，未开通广告的店没有可优化对象
    if include_optimizer and not has_ads:
        res.skipped.append(f"优化器候选跳过：{ADS_NOT_ENABLED}")
    elif include_optimizer:
        _rule_optimizer(res, sid)

    # 与 L1 同理：一个母体下几十个变体同时下滑，不合并就把整张卡占满。
    # 合并只作用于纯告警，带 intent 的建议绝不合并（见 collapse_variants）。
    res.findings = collapse_variants(res.findings)
    return res


# ── L2 日内层 ───────────────────────────────────────────────────────────────
def _budget_intent(sid: Any, cid: str, name: str, budget: float,
                   direction: int = -1) -> Optional[dict[str, Any]]:
    if budget <= 0:
        return None
    pct = threshold("stanch.max_change_pct") * direction
    return {"op_type": "campaign_budget", "sid": sid, "target_id": cid,
            "target_name": name,
            "change": {"daily_budget": round(budget * (1 + pct), 2)},
            "before": {"daily_budget": budget}}


def check_l2(sid: Any) -> CheckResult:
    """L2 日内层巡检。领星只有天粒度，靠当日累计值的多次采样做差分得到小时增量。

    接入 Amazon Marketing Stream 后这一层改由推送承载（它直接给小时数据），
    规则不变——规则读的是「小时增量」这个概念，不是采样实现。
    """
    import datetime

    from . import datasources, intraday
    datasources.install_defaults()

    res = CheckResult(sid=sid, layer="L2")
    # L2 的三条规则（花费突增/曝光归零/点击无单）全部建立在广告日内报表上，
    # 未开通广告的店整层无从谈起——短路返回，别去撞一个必然失败的接口。
    if not ads_enabled(sid):
        res.skipped.append(f"L2 日内层整层跳过：{ADS_NOT_ENABLED}")
        return res
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    base_days = [(datetime.date.today() - datetime.timedelta(days=d)).isoformat()
                 for d in range(1, 8)]

    rep = metrics.get_metric(metrics.ADS_CAMPAIGN_REPORT.key, {"sid": sid},
                            metrics.Window((today,)))
    if not rep.ok:
        res.add_gap(rep)
        return res
    res.provenance.append(f"{metrics.ADS_CAMPAIGN_REPORT.key}（当日）：{_prov(rep)}")

    cfg = metrics.get_metric(metrics.ADS_CAMPAIGN_CONFIG.key, {"sid": sid})
    conf = {str(r.get("campaign_id")): r for r in cfg.rows} if cfg.ok else {}

    sample = intraday.record_and_diff(sid, "campaign", today, rep.rows, "campaign_id")

    hist = metrics.get_metric(metrics.ADS_CAMPAIGN_REPORT.key, {"sid": sid},
                              metrics.Window(tuple(sorted(base_days))))
    hist_by_day = _agg_rows(hist.rows, "campaign_id", set(base_days)) if hist.ok else {}
    yest = _agg_rows(hist.rows, "campaign_id", {yesterday}) if hist.ok else {}

    # 规则 2：当日曝光归零（不需要增量，当日累计即可判）
    for r in rep.rows:
        cid = str(r.get("campaign_id") or "")
        y = yest.get(cid)
        if not y:
            continue
        if float(r.get("impressions") or 0) <= 0 \
                and y["impressions"] >= threshold("ads.impression_zero.min_yesterday"):
            name = str((conf.get(cid) or {}).get("name") or cid)
            res.findings.append(Finding(
                code="ads.impression_zero", layer="L2", severity=CRIT,
                action_class=ADVISORY, sid=sid, scope="campaign",
                target_id=cid, target_name=name, metric="impressions",
                current=0.0, baseline=y["impressions"], window=f"{today} 至今",
                message=f"活动「{name}」当日曝光归零（昨日 {y['impressions']:.0f}），"
                        f"请检查投放状态或竞价",
                evidence={"impressions_today": 0,
                          "impressions_yesterday": y["impressions"],
                          "serving_status": (conf.get(cid) or {}).get("serving_status")},
                provenance=_prov(rep)))

    # 规则 3：当日点击暴涨零转化
    for r in rep.rows:
        cid = str(r.get("campaign_id") or "")
        clicks, orders = float(r.get("clicks") or 0), float(r.get("orders") or 0)
        h = hist_by_day.get(cid)
        if not h or not h.get("days"):
            continue
        avg_clicks = h["clicks"] / h["days"]
        if clicks >= threshold("ads.click_no_order.min_clicks") and orders <= 0 \
                and avg_clicks > 0 and clicks >= avg_clicks * threshold("ads.click_no_order.factor"):
            c = conf.get(cid) or {}
            name = str(c.get("name") or cid)
            res.findings.append(Finding(
                code="ads.click_no_order_intraday", layer="L2", severity=WARN,
                action_class=STANCH, sid=sid, scope="campaign",
                target_id=cid, target_name=name, metric="clicks",
                current=clicks, baseline=avg_clicks, window=f"{today} 至今",
                message=f"活动「{name}」当日 {clicks:.0f} 次点击 0 转化"
                        f"（历史日均 {avg_clicks:.0f}）",
                evidence={"clicks_today": clicks, "avg_daily_clicks": round(avg_clicks, 1),
                          "spend_today": r.get("spend"), "orders_today": orders},
                provenance=_prov(rep),
                intent=_budget_intent(sid, cid, name, float(c.get("daily_budget") or 0))))

    # 规则 1：花费突增（需要增量）
    if sample.first_sample_of_day:
        res.skipped.append("花费突增规则跳过：本日首次采样，尚无增量（需至少两次采样）")
    else:
        _rule_spend_burst(res, sid, sample, conf, hist_by_day, today)

    if not sample.first_sample_of_day and not sample.observed_growth and rep.rows:
        # U8 的自动验证：当日累计值若始终不动，说明该源不滚动，L2 需改由推送承载
        res.skipped.append(
            "本次未观察到当日累计值增长；若持续如此，说明该数据源当日数据不滚动更新，"
            "L2 需改由推送源（Amazon Marketing Stream）承载")
    return res


def _rule_spend_burst(res: CheckResult, sid: Any, sample: Any,
                      conf: dict[str, Any], hist_by_day: dict[str, dict[str, float]],
                      today: str) -> None:
    """花费突增：优先用同时段历史基线，历史不足时退到日预算配速。

    退化路径必须存在——否则新装的用户前三天完全没有这条规则，
    而"刚上手那几天"恰恰是最容易配错预算烧钱的时候。
    """
    import time as _t

    from . import intraday
    hour = _t.localtime().tm_hour
    factor = threshold("ads.spend_burst.factor")

    for d in sample.deltas:
        if d.seconds < threshold("l2.min_gap_minutes") * 60:
            continue          # 采样间隔太短，增量全是噪声
        spend_delta = d.values.get("spend", 0.0)
        if spend_delta < threshold("ads.spend_burst.min_spend"):
            continue

        c = conf.get(d.entity_id) or {}
        name = str(c.get("name") or d.entity_id)
        budget = float(c.get("daily_budget") or 0)
        rate = d.per_hour("spend")

        base = intraday.rate_baseline(
            sid, "campaign", d.entity_id, hour, exclude_day=today,
            min_days=int(threshold("l2.baseline_min_days")))
        if base:
            # base 的单位是**每小时速率**（见 intraday.rate_baseline 的说明），
            # 与 rate 同量纲。这一点在采样间隔不是 1 小时的时候是死活攸关的。
            baseline_rate = base["spend"]
            basis = f"同时段历史均值（{hour:02d} 点前后）"
        elif budget > 0:
            baseline_rate = budget / 24.0
            basis = "日预算配速（历史样本不足，退化基线）"
        else:
            continue

        if baseline_rate <= 0 or rate < baseline_rate * factor:
            continue

        # 订单同步增长则不是"烧钱"，是"卖爆了"。
        # 两边都换算成**每小时**再比：基线是速率，拿区间总量去比会随采样间隔变松。
        order_delta = d.values.get("orders", 0.0)
        order_rate = d.per_hour("orders")
        base_orders = base["orders"] if base else 0.0
        if base_orders > 0 and order_rate >= base_orders * threshold("ads.spend_burst.order_tolerance"):
            continue

        res.findings.append(Finding(
            code="ads.spend_burst", layer="L2", severity=CRIT, action_class=STANCH,
            sid=sid, scope="campaign", target_id=d.entity_id, target_name=name,
            metric="spend_per_hour", current=rate, baseline=baseline_rate,
            window=f"近 {d.hours:.1f} 小时",
            message=f"活动「{name}」花费突增：近 {d.hours:.1f} 小时花 {spend_delta:.2f}"
                    f"（{rate:.2f}/时，基线 {baseline_rate:.2f}/时），订单未同步增长",
            evidence={"spend_delta": round(spend_delta, 2),
                      "hours": round(d.hours, 2),
                      "rate_per_hour": round(rate, 2),
                      "baseline_per_hour": round(baseline_rate, 2),
                      "baseline_basis": basis,
                      "orders_delta": order_delta,
                      "orders_per_hour": round(order_rate, 2),
                      "daily_budget": budget,
                      "data_corrected": d.corrected},
            provenance=f"日内采样 · {basis}",
            intent=_budget_intent(sid, d.entity_id, name, budget)))


# ── 早报汇总（方案 §5.5）────────────────────────────────────────────────────
def period_label(days: int) -> str:
    """窗口的人话名字。日报/周报/月报共用同一套装配，只有措辞不同。"""
    return {1: "昨日", 7: "本周", 30: "本月"}.get(days, f"近 {days} 日")


def period_summary(sid: Any, *, days: int = 1) -> dict[str, Any]:
    """一个窗口的关键指标 + 环比。返回 {lines, metrics, gaps}。

    ``days=1`` 是日报，7 是周报，30 是月报 —— 同一套装配，别为周报再写一遍。

    环比对照的是"再往前推同样长度的窗口"，不是"前一天"——单日波动太大，
    拿单日比单日会天天报警；周报同理，比的是上一个 7 天。
    """
    from . import datasources
    datasources.install_defaults()

    recent, base = _window_days(days)
    span = period_label(days)
    gaps: list[str] = []
    lines: list[str] = []
    out: dict[str, Any] = {}

    has_ads = ads_enabled(sid)
    rep = (metrics.get_metric(metrics.ADS_CAMPAIGN_REPORT.key, {"sid": sid},
                              metrics.Window(tuple(base + recent)))
           if has_ads else None)
    if not has_ads:
        lines.append(f"**广告**　{ADS_NOT_ENABLED}")
    elif rep.ok and rep.rows:
        cur = _agg_rows(rep.rows, "campaign_id", set(recent))
        old = _agg_rows(rep.rows, "campaign_id", set(base))

        def _tot(agg: dict[str, dict[str, float]], field: str) -> float:
            return sum(b.get(field, 0.0) for b in agg.values())

        spend, sales = _tot(cur, "spend"), _tot(cur, "sales")
        orders, clicks = _tot(cur, "orders"), _tot(cur, "clicks")
        p_spend, p_sales = _tot(old, "spend"), _tot(old, "sales")
        p_orders = _tot(old, "orders")
        acos = (spend / sales) if sales else 0.0
        p_acos = (p_spend / p_sales) if p_sales else 0.0
        out.update({"ad_spend": spend, "ad_sales": sales, "ad_orders": orders,
                    "clicks": clicks, "acos": acos})
        lines.append(f"**广告**（{span}）　花费 {spend:,.2f}（{_delta(spend, p_spend)}）"
                     f"　销售额 {sales:,.2f}（{_delta(sales, p_sales)}）")
        lines.append(f"　　　　订单 {orders:,.0f}（{_delta(orders, p_orders)}）"
                     f"　ACOS {acos:.1%}（{_delta_pp(acos, p_acos)}）")
    else:
        gaps.append(rep.gap.describe() if not rep.ok else f"{span}窗口内无广告报表数据")

    cur_p = metrics.get_metric(metrics.PROFIT_ASIN.key, {"sid": sid},
                               metrics.Window(tuple(recent)))
    old_p = metrics.get_metric(metrics.PROFIT_ASIN.key, {"sid": sid},
                               metrics.Window(tuple(base)))
    if cur_p.ok and cur_p.rows:
        total = sum(float(r.get("sales_amount") or 0) for r in cur_p.rows)
        profit = sum(float(r.get("gross_profit") or 0) for r in cur_p.rows)
        p_total = sum(float(r.get("sales_amount") or 0)
                      for r in (old_p.rows if old_p.ok else []))
        out.update({"sales_amount": total, "gross_profit": profit})
        rate = (profit / total) if total else 0.0
        lines.append(f"**店铺**（{span}）　销售额 {total:,.2f}（{_delta(total, p_total)}）"
                     f"　毛利 {profit:,.2f}（{rate:.1%}）")
    else:
        gaps.append(cur_p.gap.describe() if not cur_p.ok else f"{span}窗口内无 ASIN 利润数据")

    snap = metrics.get_metric("listing.snapshot", {"sid": sid})
    if snap.ok and snap.rows:
        rows = snap.rows
        on_sale = [r for r in rows if r.get("status_text") == "在售"]
        v_yday = sum(float(r.get("volume_yesterday") or 0) for r in rows)
        v7 = sum(float(r.get("volume_7") or 0) for r in rows)
        v30 = sum(float(r.get("volume_30") or 0) for r in rows)
        a7 = sum(float(r.get("amount_7") or 0) for r in rows)
        s7 = sum(float(r.get("spend_7") or 0) for r in rows)
        # 领星按 listing 给的是 7 日与 30 日窗口，没有"昨日 vs 前日"。
        # 所以这里比的是**近 7 日均 vs 近 30 日均**，是趋势不是日环比，标注清楚。
        avg7, avg30 = v7 / 7.0, v30 / 30.0
        out.update({"listings": len(rows), "on_sale": len(on_sale),
                    "volume_yesterday": v_yday, "volume_7": v7,
                    "amount_7": a7, "ad_spend_7": s7})
        lines.append(f"**销量**　昨日 {v_yday:,.0f} 件　近 7 日 {v7:,.0f} 件"
                     f"（日均 {avg7:.1f}，对比 30 日均 {avg30:.1f}：{_delta(avg7, avg30)}）")
        if a7:
            lines.append(f"**销售额**　近 7 日 {a7:,.2f}"
                         + (f"　广告花费 {s7:,.2f}（占比 {s7 / a7:.1%}）" if s7 else ""))
        rated = [r for r in rows if float(r.get("stars") or 0) > 0]
        low = [r for r in rated
               if float(r["stars"]) < threshold("listing.rating_low.stars")
               and float(r.get("reviews") or 0) >= threshold("listing.rating_low.min_reviews")]
        lines.append(f"**Listing**　{len(rows)} 条（在售 {len(on_sale)}）"
                     f"　有评分 {len(rated)}　评分偏低 {len(low)}")
    else:
        gaps.append(snap.gap.describe() if not snap.ok else "无 Listing 快照数据")

    inv = metrics.get_metric(metrics.INVENTORY_FBA.key, {"sid": sid})
    if inv.ok:
        fba = [r for r in inv.rows if r.get("channel") == "FBA"]
        if fba:
            oos = sum(1 for r in fba if float(r.get("fulfillable") or 0) <= 0)
            low = sum(1 for r in fba
                      if 0 < float(r.get("days_of_supply") or 0)
                      < threshold("stock.days_low.days"))
            out.update({"fba_skus": len(fba), "oos": oos, "low": low})
            lines.append(f"**库存**　FBA {len(fba)} 个 MSKU　断货 {oos}　"
                         f"可供 <{THRESHOLDS['stock.days_low.days']:.0f} 天 {low}")
        else:
            gaps.append(f"库存汇总跳过：{len(inv.rows)} 行均非 FBA 渠道")
    else:
        gaps.append(inv.gap.describe())

    out["window"] = f"{recent[0]}~{recent[-1]}" if recent else ""
    out["days"] = days
    return {"lines": lines, "metrics": out, "gaps": gaps}


def daily_summary(sid: Any, *, days: int = 1) -> dict[str, Any]:
    """早报口径的窗口汇总。保留这个名字：早报那条链路和它的测试都在用。"""
    return period_summary(sid, days=days)


def _delta(now: float, before: float) -> str:
    if not before:
        return "无对照"
    pct = (now - before) / abs(before)
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "—")
    return f"{arrow}{abs(pct):.0%}"


def _delta_pp(now: float, before: float) -> str:
    if not before:
        return "无对照"
    pp = (now - before) * 100
    arrow = "▲" if pp > 0 else ("▼" if pp < 0 else "—")
    return f"{arrow}{abs(pp):.1f}pp"


# ── Listing 快照规则（领星 MCP 源，OpenAPI 拿不到）─────────────────────────
def _rule_listing(res: CheckResult, snap: MetricResult) -> None:
    """评分 / 排名 / 价格 / 状态 / FBM 库存。

    设计要点（都是看了真实分布才定的，不是拍脑袋）：
    - **状态按跃迁报**：某店 120 条里 4 条常年「停售」，那是常态不是事件；
      只有「在售 → 停售」才值得惊动人。
    - **无评分的不报**：120 条里 100 条没有评分（新品/无评价），
      把 stars=0 当成「差评」会一次刷出 100 条。
    - **销量为零的不报缺货**：账号里大量长尾 listing 近 7 天零销量，
      对它们算「可供天数」没有意义。
    """
    rows = snap.rows
    if not rows:
        res.skipped.append("Listing 规则跳过：未取到 Listing 快照")
        return
    prov = _prov(snap)

    diff = snapshots.diff(res.sid, "listing", rows, "msku",
                          ["status_text", "stars", "rank", "price"],
                          track_membership=False)
    changes: dict[str, dict[str, Any]] = {}
    for c in diff.changes:
        changes.setdefault(c.entity_id, {})[c.field] = (c.before, c.after)
    if not diff.has_baseline:
        res.skipped.append("Listing 变更类规则跳过：本次为首轮，正在建立基线（冷启动保护）")

    for r in rows:
        msku = r.get("msku") or ""
        name = r.get("title") or r.get("asin") or msku
        short = name[:28] + ("…" if len(str(name)) > 28 else "")
        ch = changes.get(msku, {})
        # 变体合并用的分组键：同一个款的 N 个尺寸/颜色共享母体 ASIN。
        # 独立商品的 parent_asin 等于自身 asin，天然自成一组（组内 1 条，不会被合并）。
        group = str(r.get("parent_asin") or r.get("asin") or "")

        # 1. 状态跃迁：在售 → 停售
        if "status_text" in ch:
            before, after = ch["status_text"]
            if str(after) != "在售" and str(before) == "在售":
                res.findings.append(Finding(
                    code="listing.deactivated", layer="L1", severity=CRIT,
                    action_class=ADVISORY, sid=res.sid, scope="msku",
                    target_id=msku, target_name=short, metric="status_text",
                    message=f"「{short}」({r.get('asin')}) 由「在售」变为「{after}」",
                    evidence={"status_before": before, "status_after": after,
                              "asin": r.get("asin"), "price": r.get("price")},
                    provenance=prov, group_id=group))
            elif str(after) == "在售" and str(before) != "在售":
                res.findings.append(Finding(
                    code="listing.reactivated", layer="L1", severity=INFO,
                    action_class=ADVISORY, sid=res.sid, scope="msku",
                    target_id=msku, target_name=short, metric="status_text",
                    message=f"「{short}」已恢复在售（原「{before}」）",
                    evidence={"status_before": before, "status_after": after},
                    provenance=prov, group_id=group))

        # 2. 评分偏低（无评分的不报——大量新品没有评价）
        stars, reviews = float(r.get("stars") or 0), float(r.get("reviews") or 0)
        if (stars > 0 and stars < threshold("listing.rating_low.stars")
                and reviews >= threshold("listing.rating_low.min_reviews")):
            res.findings.append(Finding(
                code="listing.rating_low", layer="L1", severity=WARN,
                action_class=ADVISORY, sid=res.sid, scope="msku",
                target_id=msku, target_name=short, metric="stars",
                current=stars, baseline=threshold("listing.rating_low.stars"),
                message=f"「{short}」({r.get('asin')}) 评分 {stars:.1f} 星"
                        f"（{reviews:.0f} 条评价），低于 "
                        f"{threshold('listing.rating_low.stars')} 星",
                evidence={"stars": stars, "reviews": reviews,
                          "asin": r.get("asin"), "price": r.get("price")},
                provenance=prov, group_id=group))

        # 3. 评分下滑
        if "stars" in ch:
            before, after = float(ch["stars"][0] or 0), float(ch["stars"][1] or 0)
            if before > 0 and (before - after) >= threshold("review.rating_drop.delta"):
                res.findings.append(Finding(
                    code="review.rating_drop", layer="L1", severity=WARN,
                    action_class=ADVISORY, sid=res.sid, scope="msku",
                    target_id=msku, target_name=short, metric="stars",
                    current=after, baseline=before,
                    message=f"「{short}」评分 {before:.1f} → {after:.1f} 星，"
                            f"建议查最近的差评",
                    evidence={"stars_before": before, "stars_after": after,
                              "reviews": reviews, "asin": r.get("asin")},
                    provenance=prov, group_id=group))

        # 4. 排名恶化（rank 数值越大越差）
        if "rank" in ch:
            before, after = float(ch["rank"][0] or 0), float(ch["rank"][1] or 0)
            if before > 0 and after > before \
                    and _pct_change(after, before) >= threshold("rank.drop.pct"):
                res.findings.append(Finding(
                    code="rank.drop", layer="L1", severity=WARN,
                    action_class=ADVISORY, sid=res.sid, scope="msku",
                    target_id=msku, target_name=short, metric="rank",
                    current=after, baseline=before,
                    message=f"「{short}」大类排名 {before:.0f} → {after:.0f}"
                            f"（恶化 {_pct_change(after, before):+.0%}）",
                    evidence={"rank_before": before, "rank_after": after,
                              "asin": r.get("asin")},
                    provenance=prov, group_id=group))

        # 5. 价格被外部改动
        if "price" in ch:
            before, after = float(ch["price"][0] or 0), float(ch["price"][1] or 0)
            if before > 0 and abs(_pct_change(after, before)) >= threshold("price.changed.pct"):
                res.findings.append(Finding(
                    code="price.changed_externally", layer="L1", severity=WARN,
                    action_class=ADVISORY, sid=res.sid, scope="msku",
                    target_id=msku, target_name=short, metric="price",
                    current=after, baseline=before,
                    message=f"「{short}」售价 {before:.2f} → {after:.2f}"
                            f"（{_pct_change(after, before):+.0%}）{r.get('currency') or ''}",
                    evidence={"price_before": before, "price_after": after,
                              "currency": r.get("currency"), "asin": r.get("asin")},
                    provenance=prov, group_id=group))

        # 6. 有销量的突然断单
        avg7 = float(r.get("avg_volume_7") or 0)
        if avg7 > 0 and float(r.get("volume_yesterday") or 0) <= 0:
            res.findings.append(Finding(
                code="sales.stall", layer="L1", severity=WARN,
                action_class=ADVISORY, sid=res.sid, scope="msku",
                target_id=msku, target_name=short, metric="volume_yesterday",
                current=0.0, baseline=avg7,
                message=f"「{short}」昨日 0 单（近 7 日均 {avg7:.1f} 单/天）",
                evidence={"avg_volume_7": avg7, "volume_7": r.get("volume_7"),
                          "asin": r.get("asin"), "status": r.get("status_text")},
                provenance=prov, group_id=group))

        # 7. FBM 可供天数不足（长尾零销量的不算）
        if r.get("channel") == "FBM" and avg7 > 0:
            days = float(r.get("quantity") or 0) / avg7
            if days < threshold("stock.fbm_low.days"):
                res.findings.append(Finding(
                    code="stock.fbm_low", layer="L1",
                    severity=CRIT if days < 7 else WARN,
                    action_class=ADVISORY, sid=res.sid, scope="msku",
                    target_id=msku, target_name=short, metric="quantity",
                    current=days, baseline=threshold("stock.fbm_low.days"),
                    message=f"「{short}」自发货库存 {r.get('quantity'):.0f} 件，"
                            f"按近 7 日均销 {avg7:.1f}/天只够 {days:.1f} 天",
                    evidence={"quantity": r.get("quantity"), "avg_volume_7": avg7,
                              "days_left": round(days, 1), "asin": r.get("asin")},
                    provenance=prov, group_id=group))

    snapshots.save(res.sid, "listing", rows, "msku")


def _rule_follow_sale(res: CheckResult, follow: MetricResult) -> None:
    """跟卖监控 → Buy Box 风险。

    领星没有直接的 Buy Box 占有率接口，但「有几个卖家在跟卖」是同一件事的
    前置信号：跟卖出现就意味着 Buy Box 要分出去。**这是代理指标不是 Buy Box 本身**，
    卡片上会说清楚，别让人当成"已丢失 Buy Box"。
    """
    rows = follow.rows
    if not rows:
        res.skipped.append("跟卖监控规则跳过：未配置跟卖监控或无数据")
        return
    prov = _prov(follow)

    diff = snapshots.diff(res.sid, "follow_sale", rows, "asin",
                          ["seller_count"], track_membership=False)
    before = {c.entity_id: c.before for c in diff.changes if c.field == "seller_count"}
    if not diff.has_baseline:
        res.skipped.append("跟卖变化规则跳过：本次为首轮，正在建立基线（冷启动保护）")

    crowded = threshold("buybox.crowded.sellers")
    for r in rows:
        asin = str(r.get("asin") or "")
        name = str(r.get("title") or asin)[:28]
        n = float(r.get("seller_count") or 0)

        if asin in before:
            prev = float(before[asin] or 0)
            if n > prev:
                res.findings.append(Finding(
                    code="buybox.competitor_appeared", layer="L1",
                    severity=CRIT if prev <= 1 else WARN, action_class=ADVISORY,
                    sid=res.sid, scope="asin", target_id=asin, target_name=name,
                    metric="seller_count", current=n, baseline=prev,
                    message=f"{asin}「{name}」跟卖卖家 {prev:.0f} → {n:.0f} 家，"
                            f"Buy Box 有被分走的风险",
                    evidence={"seller_count_before": prev, "seller_count_now": n,
                              "buybox_seller": r.get("buybox_seller"),
                              "说明": "跟卖数量是 Buy Box 竞争的前置信号，不等于已丢失 Buy Box"},
                    provenance=prov))
                continue        # 已就该 ASIN 报过一条，不再叠加"拥挤"

        if n >= crowded:
            res.findings.append(Finding(
                code="buybox.crowded", layer="L1", severity=WARN,
                action_class=ADVISORY, sid=res.sid, scope="asin",
                target_id=asin, target_name=name, metric="seller_count",
                current=n, baseline=crowded,
                message=f"{asin}「{name}」有 {n:.0f} 家跟卖，Buy Box 竞争激烈",
                evidence={"seller_count": n, "buybox_seller": r.get("buybox_seller"),
                          "说明": "跟卖数量是 Buy Box 竞争的前置信号，不等于已丢失 Buy Box"},
                provenance=prov))

    snapshots.save(res.sid, "follow_sale", rows, "asin")
