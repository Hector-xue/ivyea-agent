"""亚马逊官方数据源（SP-API + Ads API）—— ADR-8 说的"换 provider 规则不用改"，
这个模块就是那句话的兑现。

它把官方响应规范化成 ``metrics.REGISTRY`` 里已经存在的 canonical 字段，
所以**一条规则都不用动**：填完凭据，原来吃领星数据的库存、广告规则
自动改吃官方数据（官方优先级 50 高于领星的 100）。

**口径对齐的两处刻意选择**（换源时数不该突然变一截）：

- 广告归因用 **7 天**（``purchases7d`` / ``sales7d``），与领星报表口径一致。
  用 14d/30d 会让同一个活动的 ACOS 在换源当天凭空变好一截。
- 库存的 ``channel`` 恒为 ``FBA``：这个接口本来就只返回 FBA 库存
  （领星那个接口会把 FBM 也混进来，规则因此必须按 channel 过滤）。
  FBM 库存要另走 Listings API，届时再补，不在这里假装有。

**没有的就是没有**：本源不提供 ``profit.asin``（亚马逊不给成本，算不出毛利）
和 ``listing.snapshot``（要拼 Listings + Catalog + 报表，属于后续工作）。
不支持就不声明 —— 指标层会如实报"没有数据源支持"，比返回一堆 0 诚实得多。
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from .. import amazon_auth, metrics
from ..metrics import Window, num, text


class AmazonSource:
    name = "amazon"
    label = "亚马逊官方 API"

    #: 指标 → 该指标的真实延迟秒数。
    #: 库存接口是近实时（分钟级）；广告报表 T+1，且当天数据一直在回填。
    _LAG = {
        metrics.INVENTORY_FBA.key: 900.0,
        metrics.ADS_CAMPAIGN_CONFIG.key: 300.0,
        metrics.ADS_CAMPAIGN_REPORT.key: 86400.0,
    }

    def supports(self, metric: str) -> bool:
        if metric not in self._LAG:
            return False
        # 广告类要有 Ads 凭据；库存类只要 SP-API 凭据。分开判是因为很多卖家
        # 先拿到 SP-API 审批、广告 API 还在排队 —— 那时库存规则就该先跑起来。
        if metric.startswith("ads."):
            return amazon_auth.is_configured(ads=True)
        return amazon_auth.is_configured()

    def lag_seconds(self, metric: str) -> float:
        return self._LAG.get(metric, 86400.0)

    def fetch(self, metric: str, scope: dict[str, Any],
              window: Optional[Window] = None) -> list[dict[str, Any]]:
        sid = scope.get("sid")
        mkt = amazon_auth.marketplace_for(sid)
        if not mkt:
            # 这个 sid 没在亚马逊侧登记站点。**抛错而不是返回空列表**：
            # 空列表会被上层当成"这个店真的没有库存"，静默且错得离谱。
            raise ValueError(
                f"sid {sid} 未在亚马逊配置里登记站点（系统配置 → 亚马逊官方 API）")

        if metric == metrics.INVENTORY_FBA.key:
            return self._inventory(mkt)
        if metric == metrics.ADS_CAMPAIGN_CONFIG.key:
            return self._campaigns(mkt)
        if metric == metrics.ADS_CAMPAIGN_REPORT.key:
            return self._campaign_report(mkt, window)
        return []

    # ── 规范化 ──────────────────────────────────────────────────────────────
    def _inventory(self, mkt: dict[str, Any]) -> list[dict[str, Any]]:
        from .. import amazon_spapi

        rows = amazon_spapi.inventory_summaries(mkt["marketplace_id"])
        out = []
        for r in rows:
            d = r.get("inventoryDetails") or {}
            unfulfillable = d.get("unfulfillableQuantity")
            # unfulfillableQuantity 是个**对象**（分 customerDamaged/warehouseDamaged…），
            # 不是数字。直接 num() 会得到 0，"不可售激增"规则就永远不触发。
            unsellable = (num(unfulfillable.get("totalUnfulfillableQuantity"))
                          if isinstance(unfulfillable, dict) else num(unfulfillable))
            reserved = d.get("reservedQuantity")
            reserved_qty = (num(reserved.get("totalReservedQuantity"))
                            if isinstance(reserved, dict) else num(reserved))
            out.append({
                "sid": mkt["sid"],
                "msku": text(r.get("sellerSku")),
                "asin": text(r.get("asin")),
                "product_name": text(r.get("productName")),
                "channel": "FBA",
                "fulfillable": num(d.get("fulfillableQuantity")),
                "inbound_shipped": num(d.get("inboundShippedQuantity")),
                "inbound_working": num(d.get("inboundWorkingQuantity")),
                "inbound_receiving": num(d.get("inboundReceivingQuantity")),
                "unsellable": unsellable,
                "reserved": reserved_qty,
                # 下面几项官方库存接口不给（要另算或走 Restock Inventory 报表）。
                # 留空而不是填 0：0 会让"可供天数不足"规则对所有 SKU 报警。
                "days_of_supply": None,
                "sell_through": None,
                "excess_qty": None,
                "min_level": None,
                "health_status": "",
                "age_365_plus": None,
            })
        return out

    def _campaigns(self, mkt: dict[str, Any]) -> list[dict[str, Any]]:
        from .. import amazon_ads

        profile = mkt.get("ads_profile_id")
        if not profile:
            raise ValueError(f"店铺 {mkt['name']} 未填广告档案 ID（ads_profile_id）")
        out = []
        for c in amazon_ads.campaigns(profile):
            budget = c.get("budget") or {}
            out.append({
                "sid": mkt["sid"],
                "campaign_id": text(c.get("campaignId")),
                "name": text(c.get("name")),
                "state": text(c.get("state")).lower(),
                # v3 把投放状态拆到了 extendedData 里；没有就留空，
                # "活动因预算耗尽停投"那条规则会自己跳过（它读的是 serving_status）
                "serving_status": text((c.get("extendedData") or {}).get("servingStatus")
                                       or c.get("servingStatus")),
                "daily_budget": num(budget.get("budget") if isinstance(budget, dict) else budget),
                "targeting_type": text(c.get("targetingType")),
                "last_updated": text((c.get("extendedData") or {}).get("lastUpdateDateTime")),
            })
        return out

    def _campaign_report(self, mkt: dict[str, Any],
                         window: Optional[Window]) -> list[dict[str, Any]]:
        from .. import amazon_ads

        profile = mkt.get("ads_profile_id")
        if not profile:
            raise ValueError(f"店铺 {mkt['name']} 未填广告档案 ID（ads_profile_id）")
        dates = sorted(window.dates) if window and window.dates else []
        if not dates:
            today = datetime.date.today()
            dates = [(today - datetime.timedelta(days=d)).isoformat() for d in (7, 1)]
        rows = amazon_ads.campaign_report(profile, dates[0], dates[-1])
        wanted = set(dates)
        out = []
        for r in rows:
            day = text(r.get("date"))
            # 报表按区间生成，窗口内的日期由规则层挑；这里先滤一遍省得下游多算
            if wanted and day not in wanted:
                continue
            out.append({
                "sid": mkt["sid"],
                "date": day,
                "campaign_id": text(r.get("campaignId")),
                "impressions": num(r.get("impressions")),
                "clicks": num(r.get("clicks")),
                # v3 叫 cost，不叫 spend（照抄 v2 字段名会拿到一列 0）
                "spend": num(r.get("cost")),
                "orders": num(r.get("purchases7d")),
                "sales": num(r.get("sales7d")),
            })
        return out
