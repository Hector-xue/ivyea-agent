"""领星 OpenAPI 数据源 —— 把领星响应规范化成 canonical 指标行。

字段映射依据：本机 2026-08-22 对真实接口的实签调用（非文档推断）。
踩过的坑，都在代码里防住了：
- 领星数值字段混用 int 与字符串（``historical_days_of_supply`` 是 "0.00"）→ 一律走 ``num()``。
- ``fba_inventory_level_health_status`` 可能是空字符串 → 规则不能对空值报警。
- FBA 库存接口把 **FBM 商品也一并返回**（实测 876/876 行为 FBM，库存全 0）。
  若不按 ``fulfillment_channel_name`` 过滤，"可售为 0"的规则会对全部 FBM 商品误报。
  因此本源在规范化时保留 ``channel``，由规则层显式过滤。
- FBA 接口的 ``sid`` 要传字符串（支持逗号分隔多店）；广告接口要传 int。
"""
from __future__ import annotations

from typing import Any, Optional

from .. import metrics
from ..lingxing_datasets import fetch_dataset
from ..metrics import Window, num, text

_PAGE = 200
_MAX_PAGES = 25


def _paged(dataset: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """领星分页取全量。单页返回不足即停。"""
    out: list[dict[str, Any]] = []
    for page in range(_MAX_PAGES):
        p = dict(params)
        p["length"] = _PAGE
        p["offset"] = page * _PAGE
        rows = fetch_dataset(dataset, p)
        out.extend(rows)
        if len(rows) < _PAGE:
            break
    return out


class LingxingSource:
    name = "lingxing"
    label = "领星 OpenAPI"

    #: 指标 → (领星数据集, 该指标的真实延迟秒数)
    _MAP: dict[str, tuple[str, float]] = {
        metrics.INVENTORY_FBA.key:        ("fba_stock", 300.0),
        metrics.ADS_CAMPAIGN_CONFIG.key:  ("sp_campaigns", 300.0),
        metrics.ADS_KEYWORD_CONFIG.key:   ("sp_keywords", 300.0),
        metrics.ADS_PRODUCT_AD_CONFIG.key: ("sp_product_ads", 300.0),
        metrics.ADS_CAMPAIGN_REPORT.key:  ("sp_campaign_report", 86400.0),
        metrics.ADS_KEYWORD_REPORT.key:   ("sp_keyword_report", 86400.0),
        metrics.ADS_SEARCH_TERM_REPORT.key: ("sp_search_term_report", 86400.0),
        metrics.PROFIT_ASIN.key:          ("asin_profit", 86400.0),
    }

    def supports(self, metric: str) -> bool:
        return metric in self._MAP

    def lag_seconds(self, metric: str) -> float:
        return self._MAP.get(metric, ("", 86400.0))[1]

    # ── 取数 ────────────────────────────────────────────────────────────────
    def fetch(self, metric: str, scope: dict[str, Any],
              window: Optional[Window] = None) -> list[dict[str, Any]]:
        if metric not in self._MAP:
            raise ValueError(f"lingxing 源不支持指标 {metric}")
        sid = scope.get("sid")
        if sid is None:
            raise ValueError("scope 缺少 sid")
        dataset = self._MAP[metric][0]

        if metric == metrics.INVENTORY_FBA.key:
            rows = _paged(dataset, {"sid": str(sid)})
            return [self._inventory(r, sid) for r in rows]
        if metric == metrics.ADS_CAMPAIGN_CONFIG.key:
            return [self._campaign(r, sid) for r in _paged(dataset, {"sid": int(sid)})]
        if metric == metrics.ADS_KEYWORD_CONFIG.key:
            return [self._keyword(r, sid) for r in _paged(dataset, {"sid": int(sid)})]
        if metric == metrics.ADS_PRODUCT_AD_CONFIG.key:
            return [self._product_ad(r, sid) for r in _paged(dataset, {"sid": int(sid)})]

        dates = list(window.dates) if window else []
        if metric == metrics.PROFIT_ASIN.key:
            if not dates:
                raise ValueError("profit.asin 需要 window.dates")
            rows = _paged(dataset, {"sids": str(sid),
                                    "startDate": dates[0], "endDate": dates[-1]})
            return [self._profit(r, sid) for r in rows]

        if not dates:
            raise ValueError(f"{metric} 需要 window.dates")
        out: list[dict[str, Any]] = []
        for day in dates:
            rows = _paged(dataset, {"sid": int(sid), "report_date": day})
            for r in rows:
                if metric == metrics.ADS_CAMPAIGN_REPORT.key:
                    out.append(self._campaign_report(r, sid, day))
                elif metric == metrics.ADS_KEYWORD_REPORT.key:
                    out.append(self._keyword_report(r, sid, day))
                else:
                    out.append(self._search_term_report(r, sid, day))
        return out

    # ── 规范化 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _inventory(r: dict[str, Any], sid: Any) -> dict[str, Any]:
        return {
            "sid": sid,
            "msku": text(r.get("msku")),
            "asin": text(r.get("asin")),
            "product_name": text(r.get("product_name")),
            # FBM/FBA —— 规则必须据此过滤，否则自发货商品会被当成断货
            "channel": text(r.get("fulfillment_channel_name")).upper(),
            "fulfillable": num(r.get("afn_fulfillable_quantity")),
            "inbound_shipped": num(r.get("afn_inbound_shipped_quantity")),
            "inbound_working": num(r.get("afn_inbound_working_quantity")),
            "inbound_receiving": num(r.get("afn_inbound_receiving_quantity")),
            "unsellable": num(r.get("afn_unsellable_quantity")),
            "reserved": num(r.get("afn_reserved_quantity")),
            # 领星已算好可供天数，不必自己推销速
            "days_of_supply": num(r.get("historical_days_of_supply")),
            "sell_through": num(r.get("sell_through")),
            "excess_qty": num(r.get("estimated_excess_quantity")),
            "min_level": num(r.get("fba_minimum_inventory_level")),
            # 实测该账号该字段为空串；规则须容忍空值
            "health_status": text(r.get("fba_inventory_level_health_status")),
            "age_365_plus": num(r.get("inv_age_365_plus_days")),
        }

    @staticmethod
    def _campaign(r: dict[str, Any], sid: Any) -> dict[str, Any]:
        return {
            "sid": sid,
            "campaign_id": text(r.get("campaign_id")),
            "name": text(r.get("name")),
            "state": text(r.get("state")).lower(),
            "serving_status": text(r.get("serving_status")).upper(),
            "daily_budget": num(r.get("daily_budget")),
            "targeting_type": text(r.get("targeting_type")),
            "last_updated": text(r.get("last_updated_date")),
        }

    @staticmethod
    def _keyword(r: dict[str, Any], sid: Any) -> dict[str, Any]:
        return {
            "sid": sid,
            "keyword_id": text(r.get("keyword_id")),
            "campaign_id": text(r.get("campaign_id")),
            "ad_group_id": text(r.get("ad_group_id")),
            "text": text(r.get("keyword_text")),
            "match_type": text(r.get("match_type")),
            "bid": num(r.get("bid")),
            "state": text(r.get("state")).lower(),
        }

    @staticmethod
    def _product_ad(r: dict[str, Any], sid: Any) -> dict[str, Any]:
        return {
            "sid": sid,
            "ad_id": text(r.get("ad_id")),
            "asin": text(r.get("asin")),
            "sku": text(r.get("sku")),
            "campaign_id": text(r.get("campaign_id")),
            "ad_group_id": text(r.get("ad_group_id")),
            "state": text(r.get("state")).lower(),
        }

    @staticmethod
    def _campaign_report(r: dict[str, Any], sid: Any, day: str) -> dict[str, Any]:
        return {
            "sid": sid, "date": day,
            "campaign_id": text(r.get("campaign_id")),
            "impressions": num(r.get("impressions")), "clicks": num(r.get("clicks")),
            "spend": num(r.get("cost")), "orders": num(r.get("orders")),
            "sales": num(r.get("sales")),
        }

    @staticmethod
    def _keyword_report(r: dict[str, Any], sid: Any, day: str) -> dict[str, Any]:
        return {
            "sid": sid, "date": day,
            "keyword_id": text(r.get("keyword_id")), "text": text(r.get("keyword_text")),
            "match_type": text(r.get("match_type")),
            "impressions": num(r.get("impressions")), "clicks": num(r.get("clicks")),
            "spend": num(r.get("cost")), "orders": num(r.get("orders")),
            "sales": num(r.get("sales")),
        }

    @staticmethod
    def _search_term_report(r: dict[str, Any], sid: Any, day: str) -> dict[str, Any]:
        return {
            "sid": sid, "date": day,
            "query": text(r.get("query")), "target_text": text(r.get("target_text")),
            "match_type": text(r.get("match_type")),
            "campaign_id": text(r.get("campaign_id")),
            "impressions": num(r.get("impressions")), "clicks": num(r.get("clicks")),
            "spend": num(r.get("cost")), "orders": num(r.get("orders")),
            "sales": num(r.get("sales")),
        }

    @staticmethod
    def _profit(r: dict[str, Any], sid: Any) -> dict[str, Any]:
        return {
            "sid": sid,
            "asin": text(r.get("asin")),
            "sales_amount": num(r.get("totalSalesAmount")),
            "ads_cost": num(r.get("totalAdsCost")),
            "gross_profit": num(r.get("grossProfit")),
            "gross_rate": num(r.get("grossRate")),
        }
