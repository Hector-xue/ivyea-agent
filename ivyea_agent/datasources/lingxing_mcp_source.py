"""领星 MCP 数据源 —— 补 OpenAPI 没有的能力面。

OpenAPI 给的是广告报表、FBA 库存、ASIN 利润；MCP 额外给了
**Listing 全量快照**（含评分/评价数/排名/价格/多窗口销量）、跟卖监控、
关键词排名、补货建议。这些是 OpenAPI 拿不到的。

三个实测踩到的契约坑，都在代码里防住了：

1. **参数名是 `sids` 不是 `sid`**（复数、逗号分隔）。领星对**未知参数静默忽略**
   （已用一个纯瞎编的参数验证过），所以传错名字不会报错，只会安静地
   返回全部店铺的数据 —— 这类 bug 最难发现。
2. **响应嵌套层级不统一**：``erp_listing`` 与 ``query_fba_valid_list`` 是
   ``data.data.list``，``query_erp_follow_sale_monitor`` 是 ``data.list``。
3. **返回的是 MCP content 里的一段文本**，多数工具是 JSON 串，
   但 ``get_my_sids`` 之类是给人看的格式化文本 —— 解析不出 JSON 时不能崩。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .. import metrics
from ..metrics import Window, num, text

log = logging.getLogger("ivyea.lingxing_mcp")

_SERVER = "lingxing"
_PAGE = 200
_MAX_PAGES = 20


class LingxingMcpError(Exception):
    pass


def _spec() -> dict[str, Any]:
    from .. import config
    spec = (config.load_mcp().get("mcpServers") or {}).get(_SERVER)
    if not spec:
        raise LingxingMcpError(f"mcp.json 里没有 '{_SERVER}' 服务器")
    return spec


def _content_text(result: Any) -> str:
    """从 MCP 工具返回里取出文本载荷。"""
    if isinstance(result, dict):
        parts = result.get("content") or []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                return str(part.get("text") or "")
    return ""


def _unwrap(payload: str) -> list[dict[str, Any]]:
    """把领星响应剥到行数组。嵌套层级各工具不一，逐层探。"""
    if not payload:
        return []
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        # 有的工具返回给人看的格式化文本（如 get_my_sids），不是 JSON。
        # 这不是错误，只是这条路取不到结构化行。
        return []
    if not isinstance(body, dict):
        return []
    code = body.get("code")
    if code not in (0, "0", None):
        raise LingxingMcpError(
            f"领星 MCP 业务错误 code={code} "
            f"msg={body.get('message')} {body.get('error_details') or ''}")

    node: Any = body.get("data")
    for _ in range(4):                       # 最多剥四层，够覆盖 data.data.list
        if isinstance(node, list):
            return [r for r in node if isinstance(r, dict)]
        if not isinstance(node, dict):
            return []
        for key in ("list", "rows", "records", "items"):
            if isinstance(node.get(key), list):
                return [r for r in node[key] if isinstance(r, dict)]
        node = node.get("data")
    return []


def call_tool(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    from ..mcp_client import MCPClient, MCPError

    client = MCPClient(_spec())
    try:
        client.initialize()
        result = client.call_tool(name, args)
    except MCPError as exc:
        raise LingxingMcpError(str(exc)) from exc
    finally:
        try:
            client.close()
        except Exception:                    # noqa: BLE001
            pass
    return _unwrap(_content_text(result))


def _paged(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(_MAX_PAGES):
        a = dict(args)
        a["length"] = _PAGE
        a["offset"] = page * _PAGE
        rows = call_tool(name, a)
        out.extend(rows)
        if len(rows) < _PAGE:
            break
    return out


# 指标定义在 metrics.py（见那里的注释：指标存不存在不该取决于哪个源被 import）
LISTING_SNAPSHOT = metrics.LISTING_SNAPSHOT
FOLLOW_SALE = metrics.FOLLOW_SALE
RESTOCK = metrics.RESTOCK


class LingxingMcpSource:
    name = "lingxing_mcp"
    label = "领星 MCP"

    _LAG = 600.0        # 领星侧聚合，比 OpenAPI 快照略陈

    def supports(self, metric: str) -> bool:
        return metric in (LISTING_SNAPSHOT.key, FOLLOW_SALE.key, RESTOCK.key)

    def lag_seconds(self, metric: str) -> float:
        return self._LAG

    def fetch(self, metric: str, scope: dict[str, Any],
              window: Optional[Window] = None) -> list[dict[str, Any]]:
        sid = scope.get("sid")
        if sid is None:
            raise LingxingMcpError("scope 缺少 sid")
        # 参数名是 sids（复数、逗号分隔）。传 sid 会被静默忽略并返回全部店铺。
        if metric == LISTING_SNAPSHOT.key:
            rows = _paged("erp_listing", {"sids": str(sid)})
            return [self._listing(r, sid) for r in rows]
        if metric == FOLLOW_SALE.key:
            rows = _paged("query_erp_follow_sale_monitor", {})
            return [self._follow(r, sid) for r in rows]
        if metric == RESTOCK.key:
            rows = _paged("query_fba_valid_list", {"sids": str(sid)})
            return [self._restock(r, sid) for r in rows]
        raise LingxingMcpError(f"不支持的指标 {metric}")

    # ── 规范化 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _listing(r: dict[str, Any], sid: Any) -> dict[str, Any]:
        return {
            "sid": r.get("store_id") or sid,
            "msku": text(r.get("msku")),
            "asin": text(r.get("asin")),
            "parent_asin": text(r.get("parent_asin")),
            "title": text(r.get("item_name")),
            "channel": text(r.get("fulfillment_channel_type")).upper(),
            "status": num(r.get("status")),
            "status_text": text(r.get("status_text")),
            "price": num(r.get("listing_price") or r.get("price")),
            "currency": text(r.get("currency_symbol") or r.get("listing_price_currency_code")),
            "stars": num(r.get("stars")),
            "reviews": num(r.get("reviews_num")),
            "rank": num(r.get("seller_rank") or r.get("rank")),
            # FBM 商品的可售数量在 quantity；FBA 的在 afn_fulfillable_quantity
            "quantity": num(r.get("quantity")),
            "fulfillable": num(r.get("afn_fulfillable_quantity")),
            "volume_yesterday": num(r.get("yesterday_volume")),
            # 领星 erp_listing 返回 116 个字段，其中**没有 seven_volume**——
            # 有 yesterday/fourteen/thirty/total_volume，唯独跳过了 7 日。
            # 但它给了 average_seven_volume，所以 7 日销量由日均反推。
            # （原先直接读 seven_volume，该字段不存在，volume_7 恒为 0。）
            "volume_7": num(r.get("seven_volume")) or num(r.get("average_seven_volume")) * 7,
            "volume_30": num(r.get("thirty_volume")),
            "avg_volume_7": num(r.get("average_seven_volume")),
            "avg_volume_30": num(r.get("average_thirty_volume")),
            "amount_7": num(r.get("seven_amount")),
            "amount_30": num(r.get("thirty_amount")),
            "spend_7": num(r.get("seven_spend")),
            "spend_30": num(r.get("thirty_spend")),
            "open_date": text(r.get("open_date_time") or r.get("open_date")),
        }

    @staticmethod
    def _follow(r: dict[str, Any], sid: Any) -> dict[str, Any]:
        return {
            "sid": sid,
            "asin": text(r.get("asin")),
            "parent_asin": text(r.get("parent_asin")),
            "title": text(r.get("title") or r.get("item_name")),
            "seller_count": num(r.get("total_seller") or r.get("follow_seller_num")),
            "buybox_seller": text(r.get("buybox_seller") or r.get("buy_box_seller")),
        }

    @staticmethod
    def _restock(r: dict[str, Any], sid: Any) -> dict[str, Any]:
        return {
            "sid": sid,
            "msku": text(r.get("msku")),
            "asin": text(r.get("asin")),
            "suggested_qty": num(r.get("suggestedQuantity") or r.get("suggested_quantity")),
            "available_days": num(r.get("availableDays") or r.get("available_days")),
            "status": text(r.get("restockStatus") or r.get("restock_status")),
        }
