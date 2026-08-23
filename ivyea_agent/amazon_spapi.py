"""SP-API 客户端（P7a）—— 签名、限流、重试、分页统一收在这里。

对齐 ``lingxing_openapi`` 的成熟做法：一个 ``_call`` 收口所有请求，
规则层与数据源层都不碰 HTTP 细节。

**几条来自文档的硬事实**（2026-08-23 核实）：

- 访问令牌走 ``x-amz-access-token`` 头，**不是** ``Authorization``。
  这一条错了的表现是 403，且错误信息不会告诉你是头名字错了。
- 2023 年起 SP-API **不再需要 AWS SigV4 签名 / IAM 角色**，
  一个 LWA access token 就够。网上大量旧教程还在教 SigV4，照抄会白写一堆。
- 分页统一是 ``nextToken`` / ``pagination.nextToken``，且**下一页请求只能带
  nextToken**（其余查询参数必须原样重发或完全不发，取决于接口），
  所以这里把翻页写成显式循环而不是让调用方自己拼。
- 限流：每个操作各自有速率（``x-amzn-RateLimit-Limit`` 头会回真实值）。
  429 一律退避重试，绝不硬打。
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from . import amazon_auth

#: 默认节流：0.5 请求/秒。比多数操作的官方速率保守，宁可慢也不要被限流拖成长尾。
_MIN_INTERVAL = 2.0
_last_call = 0.0

#: 429/5xx 的退避序列（秒）。总等待约 30 秒，超过就认输并如实报缺口 ——
#: 巡检宁可报"这次没取到"，也不要卡住整批店铺。
_BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0)


class SpApiError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _throttle() -> None:
    global _last_call
    gap = time.time() - _last_call
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_call = time.time()


def call(path: str, params: Optional[dict[str, Any]] = None, *,
         method: str = "GET", body: Optional[dict[str, Any]] = None,
         timeout: float = 40.0) -> dict[str, Any]:
    """打一次 SP-API。返回解析后的 JSON（整个响应，含 payload/pagination）。"""
    import httpx

    url = amazon_auth.spapi_host() + path
    last_err = ""
    for attempt in range(len(_BACKOFF) + 1):
        _throttle()
        token = amazon_auth.access_token(force=(attempt and "403" in last_err))
        headers = {"x-amz-access-token": token, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.request(method, url, headers=headers,
                                   params=_flatten(params), json=body)
        except httpx.HTTPError as exc:
            last_err = f"网络失败：{exc}"
            if attempt < len(_BACKOFF):
                time.sleep(_BACKOFF[attempt])
                continue
            raise SpApiError(f"SP-API {path} {last_err}") from exc

        if r.status_code in (429, 500, 502, 503, 504) and attempt < len(_BACKOFF):
            last_err = f"HTTP {r.status_code}"
            time.sleep(_BACKOFF[attempt])
            continue
        if r.status_code == 403 and attempt < len(_BACKOFF):
            # token 可能刚失效；强刷一次再试（下一轮 access_token(force=True)）
            last_err = "HTTP 403"
            time.sleep(_BACKOFF[attempt])
            continue

        try:
            data = r.json()
        except ValueError as exc:
            raise SpApiError(f"SP-API {path} 响应不可解析（HTTP {r.status_code}）",
                             r.status_code) from exc
        if r.status_code >= 400:
            errs = data.get("errors") or []
            first = errs[0] if errs else {}
            raise SpApiError(
                f"SP-API {path} 失败：{first.get('code', '')} {first.get('message', r.text[:160])}",
                r.status_code, str(first.get("code") or ""))
        return data

    raise SpApiError(f"SP-API {path} 连续失败：{last_err}")


def _flatten(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    """列表参数拼成逗号分隔 —— SP-API 收的是 ``marketplaceIds=A,B``，
    不是 httpx 默认的 ``marketplaceIds=A&marketplaceIds=B``（后者会被当成只有最后一个）。"""
    out: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if v is None or v == "":
            continue
        out[k] = ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else v
    return out


def paginate(path: str, params: dict[str, Any], *, items_at: Iterable[str],
             max_pages: int = 20) -> list[dict[str, Any]]:
    """按 nextToken 翻页并把每页的条目拼起来。

    ``items_at`` 是取条目的路径，例如 ``("payload", "inventorySummaries")``。
    **有上限**：一个店的 listing 上万条时，无限翻页会把一次巡检拖到十几分钟，
    还会撞限流。取到上限就停并如实标注（调用方据此报数据缺口）。
    """
    rows: list[dict[str, Any]] = []
    token = ""
    for _ in range(max_pages):
        page_params = dict(params)
        if token:
            page_params["nextToken"] = token
        data = call(path, page_params)
        node: Any = data
        for key in items_at:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        rows.extend(node or [])
        token = str(((data.get("pagination") or {}).get("nextToken")
                     or data.get("nextToken") or ""))
        if not token:
            break
    return rows


# ── 具体操作（字段名逐条对过官方 model 文件）──────────────────────────────────
def inventory_summaries(marketplace_id: str) -> list[dict[str, Any]]:
    """FBA 库存。

    契约来自 ``fba-inventory-api-model/fbaInventory.json``：
    ``GET /fba/inventory/v1/summaries``，必填 ``granularityType`` /
    ``granularityId`` / ``marketplaceIds``；``details=true`` 才会返回
    ``inventoryDetails``（不加的话可售/在途全是 None，看起来像"库存全是 0"）。
    """
    return paginate("/fba/inventory/v1/summaries", {
        "details": "true",
        "granularityType": "Marketplace",
        "granularityId": marketplace_id,
        "marketplaceIds": [marketplace_id],
    }, items_at=("payload", "inventorySummaries"))


def orders(marketplace_id: str, created_after: str) -> list[dict[str, Any]]:
    """订单。契约来自 ``orders-api-model/ordersV0.json``：
    ``GET /orders/v0/orders``，必填 ``MarketplaceIds``，
    且 ``CreatedAfter`` 与 ``LastUpdatedAfter`` 至少给一个。"""
    return paginate("/orders/v0/orders", {
        "MarketplaceIds": [marketplace_id],
        "CreatedAfter": created_after,
    }, items_at=("payload", "Orders"))


def item_offers(asin: str, marketplace_id: str) -> dict[str, Any]:
    """某个 ASIN 的报价（Buy Box 归属看这里）。

    ``GET /products/pricing/v0/items/{Asin}/offers``，``ItemCondition`` 必填。
    响应里 ``payload.Offers[].IsBuyBoxWinner`` 指出谁拿着购物车。
    """
    data = call(f"/products/pricing/v0/items/{asin}/offers", {
        "MarketplaceId": marketplace_id,
        "ItemCondition": "New",
    })
    return data.get("payload") or {}
