"""指标层 —— 规则消费「指标」，不消费具体接口（ADR-8）。

为什么要这一层：把规则直接写死在某个供应商的接口上，规则就被那个接口的能力绑架了。
领星只有天粒度，规则就只能天粒度；换成亚马逊官方 API 时 17 条规则要全部重写。

这一层的契约：
- **指标**（MetricSpec）声明「要什么」——库存快照、活动配置、日报表。
- **数据源**（DataSource）声明「我能给什么、延迟多少」。
- ``get_metric()`` 按优先级挑第一个支持该指标的源，返回**规范化后的行** + 溯源。

于是同一条规则今天走领星轮询、明天走亚马逊推送，代码零改动；卡片上还能显示
「来源 X · 延迟 Y」让人知道该多信任它。

数据源必须把字段名**规范化**成本模块定义的 canonical schema，否则抽象就是假的。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

# ── 粒度 ────────────────────────────────────────────────────────────────────
GRAIN_SNAPSHOT = "snapshot"   # 当前状态，无时间窗
GRAIN_HOURLY = "hourly"
GRAIN_DAILY = "daily"

# ── 实体 ────────────────────────────────────────────────────────────────────
ENTITY_MSKU = "msku"
ENTITY_ASIN = "asin"
ENTITY_CAMPAIGN = "campaign"
ENTITY_KEYWORD = "keyword"
ENTITY_AD = "ad"
ENTITY_SEARCH_TERM = "search_term"


@dataclass(frozen=True)
class MetricSpec:
    key: str
    grain: str
    entity: str
    fields: tuple[str, ...]     # canonical 字段名；数据源必须映射到这些名字
    description: str = ""


#: 指标注册表。新增指标在这里声明，数据源按 key 实现。
REGISTRY: dict[str, MetricSpec] = {}


def _reg(spec: MetricSpec) -> MetricSpec:
    REGISTRY[spec.key] = spec
    return spec


INVENTORY_FBA = _reg(MetricSpec(
    key="inventory.fba_snapshot",
    grain=GRAIN_SNAPSHOT,
    entity=ENTITY_MSKU,
    fields=("sid", "msku", "asin", "product_name", "channel",
            "fulfillable", "inbound_shipped", "inbound_working", "inbound_receiving",
            "unsellable", "reserved", "days_of_supply", "sell_through",
            "excess_qty", "min_level", "health_status", "age_365_plus"),
    description="FBA 可售/在途/不可售库存快照",
))

ADS_CAMPAIGN_CONFIG = _reg(MetricSpec(
    key="ads.campaign_config",
    grain=GRAIN_SNAPSHOT,
    entity=ENTITY_CAMPAIGN,
    fields=("sid", "campaign_id", "name", "state", "serving_status",
            "daily_budget", "targeting_type", "last_updated"),
    description="广告活动配置快照（预算/状态/投放状态）",
))

ADS_KEYWORD_CONFIG = _reg(MetricSpec(
    key="ads.keyword_config",
    grain=GRAIN_SNAPSHOT,
    entity=ENTITY_KEYWORD,
    fields=("sid", "keyword_id", "campaign_id", "ad_group_id",
            "text", "match_type", "bid", "state"),
    description="关键词配置快照（竞价/状态）",
))

ADS_PRODUCT_AD_CONFIG = _reg(MetricSpec(
    key="ads.product_ad_config",
    grain=GRAIN_SNAPSHOT,
    entity=ENTITY_AD,
    fields=("sid", "ad_id", "asin", "sku", "campaign_id", "ad_group_id", "state"),
    description="投放商品快照（把活动映射到 ASIN）",
))

ADS_CAMPAIGN_REPORT = _reg(MetricSpec(
    key="ads.campaign_report",
    grain=GRAIN_DAILY,
    entity=ENTITY_CAMPAIGN,
    fields=("sid", "date", "campaign_id", "impressions", "clicks",
            "spend", "orders", "sales"),
    description="活动日报表",
))

ADS_KEYWORD_REPORT = _reg(MetricSpec(
    key="ads.keyword_report",
    grain=GRAIN_DAILY,
    entity=ENTITY_KEYWORD,
    fields=("sid", "date", "keyword_id", "text", "match_type",
            "impressions", "clicks", "spend", "orders", "sales"),
    description="关键词日报表",
))

ADS_SEARCH_TERM_REPORT = _reg(MetricSpec(
    key="ads.search_term_report",
    grain=GRAIN_DAILY,
    entity=ENTITY_SEARCH_TERM,
    fields=("sid", "date", "query", "target_text", "match_type", "campaign_id",
            "impressions", "clicks", "spend", "orders", "sales"),
    description="搜索词日报表",
))

PROFIT_ASIN = _reg(MetricSpec(
    key="profit.asin",
    grain=GRAIN_DAILY,
    entity=ENTITY_ASIN,
    fields=("sid", "asin", "sales_amount", "ads_cost", "gross_profit", "gross_rate"),
    description="ASIN 利润",
))


# 下面三个由领星 MCP 提供（OpenAPI 拿不到）。**定义放这里而不是数据源模块**：
# 指标存不存在不该取决于哪个源恰好被 import 了 —— 否则源没配时会报
# 「未注册的指标」，而正确的说法是「没有源支持这个指标」。
LISTING_SNAPSHOT = _reg(MetricSpec(
    key="listing.snapshot",
    grain=GRAIN_SNAPSHOT,
    entity=ENTITY_MSKU,
    fields=("sid", "msku", "asin", "parent_asin", "title", "channel",
            "status", "status_text", "price", "currency", "stars", "reviews",
            "rank", "quantity", "fulfillable", "volume_yesterday",
            "volume_7", "volume_30", "avg_volume_7", "avg_volume_30",
            "amount_7", "amount_30", "spend_7", "spend_30", "open_date"),
    description="Listing 全量快照（评分/排名/价格/多窗口销量）",
))

FOLLOW_SALE = _reg(MetricSpec(
    key="monitor.follow_sale",
    grain=GRAIN_SNAPSHOT,
    entity=ENTITY_ASIN,
    fields=("sid", "asin", "parent_asin", "title", "seller_count", "buybox_seller"),
    description="ASIN 跟卖监控（卖家数量 = Buy Box 竞争信号）",
))

RESTOCK = _reg(MetricSpec(
    key="inventory.restock",
    grain=GRAIN_SNAPSHOT,
    entity=ENTITY_MSKU,
    fields=("sid", "msku", "asin", "suggested_qty", "available_days", "status"),
    description="FBA 补货建议",
))


# ── 时间窗 ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Window:
    """取数窗口。snapshot 类指标传 None。``dates`` 为 YYYY-MM-DD 列表。"""
    dates: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"dates": list(self.dates)}


# ── 溯源 ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Provenance:
    """这批数据从哪来、多新。每个 Finding 都要带上，卡片上要显示。"""
    metric: str
    source: str
    source_label: str
    grain: str
    fetched_at: float
    lag_seconds: float
    row_count: int
    scope: dict[str, Any] = field(default_factory=dict)
    window: Optional[dict[str, Any]] = None

    def describe(self) -> str:
        if self.lag_seconds < 60:
            lag = "实时"
        elif self.lag_seconds < 3600:
            lag = f"延迟约 {int(self.lag_seconds // 60)} 分钟"
        elif self.lag_seconds < 86400:
            lag = f"延迟约 {int(self.lag_seconds // 3600)} 小时"
        else:
            lag = f"延迟约 {int(self.lag_seconds // 86400)} 天"
        return f"来源 {self.source_label} · {lag} · {self.row_count} 行"


@dataclass(frozen=True)
class DataGap:
    """没取到数。**不是异常**——规则照常存在，只是这次没数据可判。

    绝不能把 gap 静默吞掉：它要出现在巡检结果里，让人知道哪条规则没跑、为什么。
    """
    metric: str
    reason: str
    tried: tuple[str, ...] = ()

    def describe(self) -> str:
        tried = f"（已尝试：{', '.join(self.tried)}）" if self.tried else ""
        return f"指标 {self.metric} 无数据：{self.reason}{tried}"


@dataclass(frozen=True)
class MetricResult:
    metric: str
    rows: list[dict[str, Any]]
    provenance: Optional[Provenance] = None
    gap: Optional[DataGap] = None

    @property
    def ok(self) -> bool:
        return self.gap is None


# ── 数据源协议 ──────────────────────────────────────────────────────────────
@runtime_checkable
class DataSource(Protocol):
    name: str
    label: str

    def supports(self, metric: str) -> bool: ...

    def lag_seconds(self, metric: str) -> float:
        """该源对该指标的**真实**延迟（秒）。快照类接近 0，T+1 报表约 86400。"""
        ...

    def fetch(self, metric: str, scope: dict[str, Any],
              window: Optional[Window] = None) -> list[dict[str, Any]]:
        """返回**规范化后**的行（字段名对齐 MetricSpec.fields）。"""
        ...


_SOURCES: list[tuple[int, DataSource]] = []


def register(source: DataSource, *, priority: int = 100) -> None:
    """注册数据源。priority 小者优先——推送类应比轮询类小。"""
    unregister(source.name)
    _SOURCES.append((priority, source))
    _SOURCES.sort(key=lambda t: t[0])


def unregister(name: str) -> None:
    global _SOURCES
    _SOURCES = [(p, s) for p, s in _SOURCES if s.name != name]


def registered() -> list[DataSource]:
    return [s for _, s in _SOURCES]


def sources_for(metric: str) -> list[DataSource]:
    return [s for _, s in _SOURCES if s.supports(metric)]


def get_metric(metric: str, scope: Optional[dict[str, Any]] = None,
               window: Optional[Window] = None) -> MetricResult:
    """按优先级取指标。任何一个源成功即返回；全失败返回带 gap 的结果。"""
    scope = dict(scope or {})
    if metric not in REGISTRY:
        return MetricResult(metric, [], gap=DataGap(metric, "未注册的指标"))

    candidates = sources_for(metric)
    if not candidates:
        return MetricResult(metric, [], gap=DataGap(
            metric, "没有任何已注册数据源支持该指标",
            tuple(s.name for s in registered())))

    errors: list[str] = []
    for src in candidates:
        try:
            rows = src.fetch(metric, scope, window)
        except Exception as exc:                     # noqa: BLE001 —— 换下一个源
            errors.append(f"{src.name}: {exc}")
            continue
        prov = Provenance(
            metric=metric, source=src.name, source_label=src.label,
            grain=REGISTRY[metric].grain, fetched_at=time.time(),
            lag_seconds=float(src.lag_seconds(metric)), row_count=len(rows),
            scope=scope, window=window.as_dict() if window else None,
        )
        return MetricResult(metric, rows, provenance=prov)

    reason = "；".join(errors) if errors else "所有数据源均返回失败"
    return MetricResult(metric, [], gap=DataGap(
        metric, reason, tuple(s.name for s in candidates)))


# ── 数值工具（数据源与规则共用）─────────────────────────────────────────────
def num(value: Any, default: float = 0.0) -> float:
    """领星的数值字段有 int 也有字符串（"0.00"），统一转换。"""
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()
