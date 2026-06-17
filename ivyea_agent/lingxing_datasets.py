"""领星广告数据集注册 + 取数（agent 自有，直连领星 OpenAPI）。

路由/参数移植自 ivyea-ops `lingxing_data.py` 的 READ_DATASETS（广告+利润子集）。
M1 不做缓存层，直连取数；行抽取兼容 data 为 list 或 data.{list,records,rows,items}。
"""
from __future__ import annotations

from typing import Any, Optional

from .lingxing_openapi import LingXingError, call

# 数据集 → {route, method, 必填校验交给上层}
DATASETS: dict[str, dict[str, Any]] = {
    "sp_search_term_report": {"route": "/pb/openapi/newad/queryWordReports", "method": "POST"},
    "sp_keyword_report":     {"route": "/pb/openapi/newad/spKeywordReports", "method": "POST"},
    "sp_campaign_report":    {"route": "/pb/openapi/newad/spCampaignReports", "method": "POST"},
    "sp_keywords":           {"route": "/pb/openapi/newad/spKeywords", "method": "POST"},
    "sp_campaigns":          {"route": "/pb/openapi/newad/spCampaigns", "method": "POST"},
    "sp_product_ads":        {"route": "/pb/openapi/newad/spProductAds", "method": "POST"},
    "asin_profit":           {"route": "/bd/profit/statistics/open/asin/list", "method": "POST"},
}

SELLER_ROUTE = "/erp/sc/data/seller/lists"


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("list", "records", "rows", "items"):
            if isinstance(data.get(k), list):
                return [r for r in data[k] if isinstance(r, dict)]
    return []


def fetch_dataset(name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """调一个数据集（单页/单日），返回行数组。未知数据集抛错。"""
    spec = DATASETS.get(name)
    if not spec:
        raise LingXingError(f"未知数据集: {name}")
    payload = call(spec["route"], params, method=spec["method"])
    return _extract_rows(payload)


def list_sellers() -> list[dict[str, Any]]:
    """店铺列表（无副作用）。返回 [{sid,name,country,marketplace,...}]。"""
    payload = call(SELLER_ROUTE, {}, method="GET")
    data = payload.get("data")
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
