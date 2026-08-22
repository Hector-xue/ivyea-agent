"""数据源实现（ADR-8）。

每个模块实现 ``metrics.DataSource`` 协议，把某个供应商的原始响应**规范化**成
``metrics.REGISTRY`` 定义的 canonical 字段名。规则层只认 canonical 名字，
所以换供应商时规则代码零改动。

优先级约定（数字小者优先）：
  10  推送类（亚马逊 SP-API Notifications / Marketing Stream）—— 待 P7 接入
  50  官方轮询（SP-API / Ads API）—— 待 P7 接入
  100 领星 OpenAPI —— 已接入，兜底且提供领星特有数据（成本/利润/采购）
"""
from __future__ import annotations

PRIORITY_PUSH = 10
PRIORITY_OFFICIAL = 50
PRIORITY_LINGXING = 100


def install_defaults() -> None:
    """注册当前可用的数据源。幂等，可重复调用。"""
    from .. import config, metrics
    from .lingxing_source import LingxingSource

    metrics.register(LingxingSource(), priority=PRIORITY_LINGXING)

    # 领星 MCP 补 OpenAPI 没有的能力面（Listing 快照/跟卖/补货）。
    # 没配就不注册 —— 缺源时指标层会报「数据缺口」，比注册一个必然失败的源清楚。
    if (config.load_mcp().get("mcpServers") or {}).get("lingxing"):
        from .lingxing_mcp_source import LingxingMcpSource
        metrics.register(LingxingMcpSource(), priority=PRIORITY_LINGXING + 1)
