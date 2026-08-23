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

import re as _re
from typing import Any, Optional

from .. import metrics
from ..lingxing_datasets import fetch_dataset
from ..metrics import Window, num, text

_MONEY_RE = _re.compile(r"-?[\d.]+")


def _money(value: Any) -> Optional[float]:
    """促销接口的金额带货币符号（``budget`` 是 "JP¥10,084.0"，``cost`` 是 "0.00"）。

    通用的 ``num()`` 只会剥逗号和百分号，遇到货币前缀直接落回默认值 —— 于是
    预算永远是 None，"预算见底"这条规则永远不会触发。**解析不出来返回 None
    而不是 0**："没有预算这个概念"和"预算是 0"在卡片上必须能区分开。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    hit = _MONEY_RE.search(str(value).replace(",", "").replace("，", ""))
    if not hit:
        return None
    try:
        return float(hit.group(0))
    except ValueError:
        return None

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
        # 促销：接口本身没有日期延迟（查什么窗口给什么窗口），但**数据是浏览器
        # 插件同步进领星的**，真实新鲜度取决于插件在不在线。所以这里的延迟按
        # "插件正常时的同步周期"给 1 小时，每行还额外带 sync_age_hours，让规则
        # 能对"插件掉线导致数据停更"单独报警。
        metrics.PROMOTION_ACTIVE.key:     ("promo_coupon", 3600.0),
    }

    #: 四类促销共用同一形状，只有数据集不同。
    _PROMO_KINDS = (("coupon", "promo_coupon"), ("seckill", "promo_seckill"),
                    ("manage", "promo_manage"), ("vip_discount", "promo_vip_discount"))

    #: 平台原始状态 → 是否还在"会发生变化"的链路上。取消/过期/失败的活动没有
    #: "还剩多久"可言，不该混进倒计时。
    _PROMO_DEAD = {"CANCELED", "CANCELLED", "EXPIRED", "ENDED", "FAILED", "DISMISSED"}

    #: listingList 的 category 编码 → kind
    _PROMO_CATEGORY = {1: "coupon", 2: "seckill", 3: "manage", 4: "vip_discount"}

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
        if metric == metrics.PROMOTION_ACTIVE.key:
            return self._promotions(sid)

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

    # ── 促销 ────────────────────────────────────────────────────────────────
    def _promotions(self, sid: Any) -> list[dict[str, Any]]:
        """四类活动 + ASIN 维度 → 统一的促销行。

        ASIN 是从 ``promo_listing`` 按 promotion_id 反挂回来的：**活动列表接口
        不返回 ASIN**，而"哪个 ASIN 的券要结束了"正是这条规则要回答的问题。

        窗口固定「过去 30 天 ~ 未来 59 天」= 90 天，贴着领星单次查询的跨度上限。
        两头都要留：已经开始的活动 start_date 在过去，只查未来就一条都看不见。
        """
        import datetime as _dt

        from .. import stores

        tz = stores.tzinfo(sid)
        today = _dt.date.today()
        window = {"start_date": (today - _dt.timedelta(days=30)).isoformat(),
                  "end_date": (today + _dt.timedelta(days=59)).isoformat(),
                  "sids": [int(sid)]}

        asin_index = self._promo_asins(sid, today)
        out: list[dict[str, Any]] = []
        for kind, dataset in self._PROMO_KINDS:
            try:
                rows = _paged(dataset, dict(window))
            except Exception:                            # noqa: BLE001
                # 一类活动取不到不该让另外三类也没有。少一类比一条都没有强。
                continue
            for r in rows:
                row = self._promotion(r, sid, kind, tz)
                row["asins"] = asin_index.get(row["promotion_id"], [])
                row["asin_count"] = len(row["asins"])
                out.append(row)
        return out

    def _promo_asins(self, sid: Any, today: Any) -> dict[str, list[str]]:
        import datetime as _dt
        try:
            rows = _paged("promo_listing", {
                "site_date": today.isoformat(),
                "start_time": (today - _dt.timedelta(days=30)).isoformat(),
                "end_time": (today + _dt.timedelta(days=59)).isoformat(),
                "sids": [int(sid)], "status": [0, 1, 2, 3],
                "product_status": [1], "promotion_category": [1, 2, 3, 4],
            })
        except Exception:                                # noqa: BLE001
            return {}
        index: dict[str, list[str]] = {}
        for r in rows:
            asin = text(r.get("asin"))
            if not asin:
                continue
            for promo in (r.get("promotion_list") or []):
                pid = text(promo.get("promotion_id"))
                if not pid:
                    continue
                bucket = index.setdefault(pid, [])
                if asin not in bucket:
                    bucket.append(asin)
        return index

    @classmethod
    def _promotion(cls, r: dict[str, Any], sid: Any, kind: str, tz: Any) -> dict[str, Any]:
        import datetime as _dt

        def parse(raw: Any) -> Optional[_dt.datetime]:
            value = text(raw)
            if not value or value.startswith("0000"):
                return None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return _dt.datetime.strptime(value, fmt).replace(tzinfo=tz)
                except ValueError:
                    continue
            return None

        now = _dt.datetime.now(_dt.timezone.utc)
        start, end = parse(r.get("promotion_start_time")), parse(r.get("promotion_end_time"))
        synced = parse(r.get("last_sync_time"))
        status = text(r.get("origin_status")).upper()
        alive = status not in cls._PROMO_DEAD

        if not alive:
            phase = "closed"
        elif end and now >= end:
            phase = "ended"
        elif start and now < start:
            phase = "upcoming"
        elif start or end:
            phase = "running"
        else:
            phase = "unknown"

        budget, cost = _money(r.get("budget")), _money(r.get("cost"))
        used = (round(cost / budget * 100, 1)
                if budget and budget > 0 and cost is not None else None)
        return {
            "sid": sid,
            "promotion_id": text(r.get("promotion_id")),
            "kind": kind,
            "name": text(r.get("name")) or text(r.get("description")) or "(未命名)",
            "status": status,
            "currency": text(r.get("currency_icon")),
            "start_at": start.isoformat() if start else "",
            "end_at": end.isoformat() if end else "",
            "start_local": text(r.get("promotion_start_time")),
            "end_local": text(r.get("promotion_end_time")),
            "tz": str(getattr(tz, "key", tz)),
            "seconds_to_start": int((start - now).total_seconds()) if start else None,
            "seconds_to_end": int((end - now).total_seconds()) if end else None,
            "phase": phase,
            "budget": budget,
            "cost": cost,
            "budget_used_pct": used,
            "sales_amount": _money(r.get("sales_amount")) or 0.0,
            "sales_volume": _money(r.get("sales_volume")) or 0.0,
            "asins": [],
            "asin_count": 0,
            "last_sync_at": synced.isoformat() if synced else "",
            "sync_age_hours": (round((now - synced).total_seconds() / 3600, 1)
                               if synced else None),
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
