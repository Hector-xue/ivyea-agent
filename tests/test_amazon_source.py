"""亚马逊官方 API 接入（P7a）。

这台机器没有亚马逊卖家账号，所以用 fixtures + 打桩 HTTP 驱动 —— 但 fixtures 的
**字段名逐条取自官方 model 文件**（2026-08-23 拉取核对）：

- `amzn/selling-partner-api-models` 的 `fbaInventory.json` / `ordersV0.json`
- `amzn/ads-advanced-tools-docs` 的官方 Postman 集合（headers / 媒体类型 / 报表 body）

用它们守住的是"接上真账号那天不会返工"的部分：路径、参数名、请求头、
响应字段名、以及规范化后必须落在 canonical 契约上。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture()
def amz(ivyea_home, monkeypatch):
    """配好一套假凭据 + 两个站点。"""
    import importlib

    from ivyea_agent import amazon_auth
    importlib.reload(amazon_auth)
    monkeypatch.setenv(amazon_auth.ENV_CLIENT_ID, "amzn1.application-oa2-client.x")
    monkeypatch.setenv(amazon_auth.ENV_CLIENT_SECRET, "secret")
    monkeypatch.setenv(amazon_auth.ENV_REFRESH_TOKEN, "Atzr|refresh")
    amazon_auth.configure({"marketplaces": [
        {"sid": "1863", "marketplace_id": "A1F83G8C2ARO7P", "name": "欧洲-UK",
         "ads_profile_id": "111"},
        {"sid": "9001", "marketplace_id": "ATVPDKIKX0DER", "name": "美国"},
    ]})
    # token 一律现成的，避免每个用例都去打 LWA
    monkeypatch.setattr(amazon_auth, "access_token", lambda **k: "atoken")
    return amazon_auth


# ── 区域与站点 ──────────────────────────────────────────────────────────────
def test_region_is_derived_from_the_marketplace(amz):
    """让人去背"英国属于 eu 还是 na"是没必要的一步。"""
    assert amz.region() == "eu"
    assert amz.spapi_host() == "https://sellingpartnerapi-eu.amazon.com"
    assert amz.ads_host() == "https://advertising-api-eu.amazon.com"


def test_ads_credentials_fall_back_to_the_spapi_app(amz, monkeypatch):
    """绝大多数卖家两边用同一个应用；强迫填两遍只会填错一遍。"""
    assert amz.creds(ads=True)[0] == "amzn1.application-oa2-client.x"
    monkeypatch.setenv(amz.ENV_ADS_CLIENT_ID, "ads-client")
    monkeypatch.setenv(amz.ENV_ADS_CLIENT_SECRET, "ads-secret")
    monkeypatch.setenv(amz.ENV_ADS_REFRESH_TOKEN, "ads-refresh")
    assert amz.creds(ads=True)[0] == "ads-client"


def test_status_never_leaks_secrets(amz):
    blob = json.dumps(amz.status(), ensure_ascii=False)
    assert "secret" not in blob and "Atzr" not in blob


def test_blank_fields_do_not_wipe_the_config(amz):
    """界面上没填的框会传空串。当成清除的话，打开配置页保存一下凭据就没了。"""
    amz.configure({"seller_id": "A23SU2M9XL8R0O"})
    amz.configure({"seller_id": ""})
    assert amz.status()["seller_id"] == "A23SU2M9XL8R0O"
    amz.configure({"seller_id": "", "clear": ["seller_id"]})
    assert amz.status()["seller_id"] == ""


# ── SP-API 客户端 ───────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


def _fake_client(monkeypatch, module, handler):
    """把 httpx.Client 换成一个记录请求并按 handler 回包的假货。"""
    calls: list[dict] = []

    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, params=None, json=None):
            calls.append({"method": method, "url": url, "headers": headers or {},
                          "params": params or {}, "body": json})
            return handler(calls[-1])

        def get(self, url, headers=None, **k):
            return self.request("GET", url, headers=headers)

        def post(self, url, headers=None, json=None, data=None, **k):
            calls.append({"method": "POST", "url": url, "headers": headers or {},
                          "params": {}, "body": json or data})
            return handler(calls[-1])

    monkeypatch.setattr(module, "Client", _C)
    return calls


def test_spapi_uses_the_right_header_and_flattens_list_params(amz, monkeypatch):
    """两个都错过就白写一天：

    - 令牌走 x-amz-access-token，不是 Authorization（错了拿 403，且不告诉你原因）；
    - 列表参数要拼成 ``a,b``；httpx 默认展开成重复 key，SP-API 只认最后一个。
    """
    import httpx

    from ivyea_agent import amazon_spapi

    monkeypatch.setattr(amazon_spapi, "_MIN_INTERVAL", 0.0)
    calls = _fake_client(monkeypatch, httpx, lambda c: _Resp(
        {"payload": {"inventorySummaries": []}}))
    amazon_spapi.inventory_summaries("A1F83G8C2ARO7P")

    req = calls[0]
    assert req["headers"]["x-amz-access-token"] == "atoken"
    assert "Authorization" not in req["headers"]
    assert req["params"]["marketplaceIds"] == "A1F83G8C2ARO7P"
    assert req["params"]["details"] == "true"          # 不加就没有 inventoryDetails
    assert req["url"].startswith("https://sellingpartnerapi-eu.amazon.com")


def test_spapi_paginates_by_next_token(amz, monkeypatch):
    import httpx

    from ivyea_agent import amazon_spapi

    monkeypatch.setattr(amazon_spapi, "_MIN_INTERVAL", 0.0)
    pages = [
        {"payload": {"inventorySummaries": [{"sellerSku": "A"}]},
         "pagination": {"nextToken": "t2"}},
        {"payload": {"inventorySummaries": [{"sellerSku": "B"}]}},
    ]
    seen = []

    def _handler(req):
        seen.append(req["params"].get("nextToken"))
        return _Resp(pages[len(seen) - 1])

    _fake_client(monkeypatch, httpx, _handler)
    rows = amazon_spapi.inventory_summaries("A1F83G8C2ARO7P")
    assert [r["sellerSku"] for r in rows] == ["A", "B"]
    assert seen == [None, "t2"]


def test_spapi_retries_then_reports_instead_of_hanging(amz, monkeypatch):
    """限流一律退避重试；退到底就如实报错 —— 巡检不能被一个店卡住。"""
    import httpx

    from ivyea_agent import amazon_spapi

    monkeypatch.setattr(amazon_spapi, "_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(amazon_spapi, "_BACKOFF", (0.0, 0.0))
    _fake_client(monkeypatch, httpx, lambda c: _Resp({"errors": [
        {"code": "QuotaExceeded", "message": "too many"}]}, status=429))
    with pytest.raises(amazon_spapi.SpApiError):
        amazon_spapi.inventory_summaries("A1F83G8C2ARO7P")


# ── 规范化：落在 canonical 契约上 ───────────────────────────────────────────
_SUMMARY = {
    "asin": "B01", "fnSku": "X1", "sellerSku": "MSKU-1", "condition": "NewItem",
    "productName": "测试品", "totalQuantity": 30,
    "inventoryDetails": {
        "fulfillableQuantity": 12,
        "inboundWorkingQuantity": 1,
        "inboundShippedQuantity": 5,
        "inboundReceivingQuantity": 2,
        # 这两个在官方 model 里是**对象**，不是数字
        "unfulfillableQuantity": {"totalUnfulfillableQuantity": 4,
                                  "customerDamagedQuantity": 4},
        "reservedQuantity": {"totalReservedQuantity": 6},
    },
}


def test_inventory_maps_onto_the_canonical_fields(amz, monkeypatch):
    from ivyea_agent import amazon_spapi, metrics
    from ivyea_agent.datasources.amazon_source import AmazonSource

    monkeypatch.setattr(amazon_spapi, "inventory_summaries", lambda mid: [_SUMMARY])
    rows = AmazonSource().fetch(metrics.INVENTORY_FBA.key, {"sid": "1863"})
    r = rows[0]
    assert r["sid"] == "1863" and r["msku"] == "MSKU-1" and r["asin"] == "B01"
    assert r["fulfillable"] == 12 and r["inbound_shipped"] == 5
    assert r["channel"] == "FBA"
    # 对象型字段必须取到里面的合计 —— 直接 num() 会得到 0，
    # "不可售激增"那条规则就永远不触发（静默失效，最难查的那种）
    assert r["unsellable"] == 4 and r["reserved"] == 6
    # 官方接口不给可供天数：留 None 而不是 0，否则"可供天数不足"会对所有 SKU 报警
    assert r["days_of_supply"] is None
    assert set(r).issuperset(set(metrics.INVENTORY_FBA.fields))


def test_unknown_sid_raises_instead_of_returning_nothing(amz):
    from ivyea_agent import metrics
    from ivyea_agent.datasources.amazon_source import AmazonSource

    with pytest.raises(ValueError):
        AmazonSource().fetch(metrics.INVENTORY_FBA.key, {"sid": "不存在"})


def test_campaign_report_uses_v3_column_names(amz, monkeypatch):
    """v3 把花费叫 cost（v2 是 spend）。照抄 v2 会拿到一整列 0 —— 不报错，只是全是 0。"""
    from ivyea_agent import amazon_ads, metrics
    from ivyea_agent.datasources.amazon_source import AmazonSource

    monkeypatch.setattr(amazon_ads, "campaign_report", lambda p, s, e: [
        {"date": "2026-08-20", "campaignId": 123, "impressions": 900,
         "clicks": 40, "cost": 12.5, "purchases7d": 3, "sales7d": 90.0},
    ])
    rows = AmazonSource().fetch(metrics.ADS_CAMPAIGN_REPORT.key, {"sid": "1863"},
                                metrics.Window(("2026-08-20",)))
    assert rows == [{"sid": "1863", "date": "2026-08-20", "campaign_id": "123",
                     "impressions": 900.0, "clicks": 40.0, "spend": 12.5,
                     "orders": 3.0, "sales": 90.0}]


def test_ads_metrics_need_the_profile_id(amz):
    from ivyea_agent import metrics
    from ivyea_agent.datasources.amazon_source import AmazonSource

    # 9001（美国）没填广告档案 → 明确报错，而不是安静地返回空
    with pytest.raises(ValueError):
        AmazonSource().fetch(metrics.ADS_CAMPAIGN_CONFIG.key, {"sid": "9001"})


def test_ads_support_is_independent_of_spapi_support(amz, monkeypatch):
    """SP-API 先批下来、广告 API 还在排队，是很常见的中间态：
    那时库存规则就该先跑起来，不该被广告拖着一起停。"""
    from ivyea_agent import amazon_auth, metrics
    from ivyea_agent.datasources.amazon_source import AmazonSource

    src = AmazonSource()
    assert src.supports(metrics.INVENTORY_FBA.key)
    monkeypatch.setattr(amazon_auth, "is_configured",
                        lambda ads=False: not ads)
    assert src.supports(metrics.INVENTORY_FBA.key)
    assert src.supports(metrics.ADS_CAMPAIGN_REPORT.key) is False


def test_official_source_outranks_lingxing(amz, monkeypatch):
    """官方是第一手、延迟更低；领星是转手数据。同一指标官方优先。"""
    from ivyea_agent import datasources, metrics

    for s in list(metrics.registered()):
        metrics.unregister(s.name)
    datasources.install_defaults()
    names = [s.name for s in metrics.sources_for(metrics.INVENTORY_FBA.key)]
    assert names and names[0] == "amazon"
    for s in list(metrics.registered()):
        metrics.unregister(s.name)


def test_unconfigured_install_registers_no_amazon_source(ivyea_home, monkeypatch):
    """没配凭据还注册的话，每条规则都多出一行"取数失败"，把真问题淹掉。"""
    from ivyea_agent import amazon_auth, datasources, metrics

    monkeypatch.setattr(amazon_auth, "is_configured", lambda ads=False: False)
    for s in list(metrics.registered()):
        metrics.unregister(s.name)
    datasources.install_defaults()
    assert "amazon" not in [s.name for s in metrics.registered()]
    for s in list(metrics.registered()):
        metrics.unregister(s.name)


# ── 店铺清单：只用亚马逊的人也要能巡检 ──────────────────────────────────────
def test_store_list_falls_back_to_amazon_marketplaces(amz, monkeypatch):
    """不做这一步，只用亚马逊官方 API 的人一个店都巡检不了：
    目标解析拿不到清单，每条任务都报"缺少 sid"。"""
    from ivyea_agent import lingxing_datasets, stores

    def _no_lingxing():
        raise RuntimeError("领星未配置")

    monkeypatch.setattr(lingxing_datasets, "list_sellers", _no_lingxing)
    rows = stores.list_stores(force=True)
    assert {r["sid"] for r in rows} == {"1863", "9001"}
    uk = next(r for r in rows if r["sid"] == "1863")
    assert uk["has_ads"] is True and uk["country"] == "UK"
    # 没填广告档案的站点按"未开通广告"处理 —— 与领星的标志位同一语义，
    # 广告规则会跳过而不是当成故障（ADR-0018）
    assert next(r for r in rows if r["sid"] == "9001")["has_ads"] is False
