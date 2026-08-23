"""Listing 快照规则（领星 MCP 源）。

阈值与"报不报"的判断都是**看了真实分布**才定的：某店 120 条里
116 在售 / 4 停售、100 条无评分、0 条近 7 天有销量。
"""
from __future__ import annotations

import json

import pytest


def _raw(**over):
    """一行 erp_listing 原始响应（字段名取自实调）。"""
    row = {
        "store_id": 1863, "msku": "M1", "asin": "B01", "parent_asin": "B01",
        "item_name": "测试商品", "fulfillment_channel_type": "FBM",
        "status": 1, "status_text": "在售",
        "listing_price": "35.66", "currency_symbol": "GBP",
        "stars": 5, "reviews_num": 12, "seller_rank": 100000,
        "quantity": "50", "afn_fulfillable_quantity": 0,
        "yesterday_volume": "2", "seven_volume": "14", "thirty_volume": "60",
        "average_seven_volume": "2.0", "average_thirty_volume": "2.0",
        "seven_amount": "100.00", "thirty_amount": "400.00",
        "seven_spend": "10.00", "thirty_spend": "40.00",
        "open_date_time": "2022-04-23 13:11:57 +03:00",
    }
    row.update(over)
    return row


class _Src:
    name, label = "fake_mcp", "测试 MCP"

    def __init__(self, rows):
        self.rows = rows

    def supports(self, m):
        return m == "listing.snapshot"

    def lag_seconds(self, m):
        return 600.0

    def fetch(self, m, scope, window=None):
        from ivyea_agent.datasources.lingxing_mcp_source import LingxingMcpSource
        return [LingxingMcpSource._listing(r, scope.get("sid")) for r in self.rows]


@pytest.fixture()
def wire(ivyea_home, monkeypatch):
    from ivyea_agent import metrics, datasources

    def _install(rows):
        for s in list(metrics.registered()):
            metrics.unregister(s.name)
        metrics.register(_Src(rows), priority=1)
        monkeypatch.setattr(datasources, "install_defaults", lambda: None)
    yield _install
    from ivyea_agent import metrics as m
    for s in list(m.registered()):
        m.unregister(s.name)


def _codes(res):
    return sorted(f.code for f in res.findings)


def _run(sid=1):
    from ivyea_agent import store_health
    return store_health.check_l1(sid)


# ── 规范化 ──────────────────────────────────────────────────────────────────
def test_normalization_handles_string_numbers(ivyea_home):
    from ivyea_agent.datasources.lingxing_mcp_source import LingxingMcpSource

    row = LingxingMcpSource._listing(_raw(), 1863)
    assert row["price"] == 35.66 and row["quantity"] == 50.0
    assert row["avg_volume_7"] == 2.0 and row["channel"] == "FBM"


def test_unwrap_handles_both_nesting_depths(ivyea_home):
    """erp_listing 是 data.data.list，跟卖监控是 data.list —— 层级不统一。"""
    from ivyea_agent.datasources import lingxing_mcp_source as src

    deep = json.dumps({"code": 0, "data": {"data": {"list": [{"msku": "A"}]}}})
    flat = json.dumps({"code": 0, "data": {"list": [{"msku": "B"}]}})
    assert src._unwrap(deep)[0]["msku"] == "A"
    assert src._unwrap(flat)[0]["msku"] == "B"


def test_unwrap_prose_response_is_not_an_error(ivyea_home):
    """get_my_sids 之类返回给人看的文本，解析不出 JSON 不能崩。"""
    from ivyea_agent.datasources import lingxing_mcp_source as src

    assert src._unwrap("店铺列表:\n- sid: 1872") == []


def test_unwrap_raises_on_business_error(ivyea_home):
    from ivyea_agent.datasources import lingxing_mcp_source as src

    with pytest.raises(src.LingxingMcpError):
        src._unwrap(json.dumps({"code": 102, "message": "参数不合法"}))


# ── 评分 ────────────────────────────────────────────────────────────────────
def test_low_rating_fires(wire):
    wire([_raw(stars=2.9, reviews_num=5)])
    hits = [f for f in _run().findings if f.code == "listing.rating_low"]
    assert len(hits) == 1 and hits[0].current == 2.9


def test_no_rating_never_fires(wire):
    """120 条里 100 条没有评分。把 stars=0 当差评会一次刷出 100 条。"""
    wire([_raw(stars=0, reviews_num=0) for _ in range(20)])
    assert "listing.rating_low" not in _codes(_run())


def test_few_reviews_never_fires(wire):
    """评价太少时评分不稳定，一条差评就 1 星。"""
    wire([_raw(stars=1.0, reviews_num=1)])
    assert "listing.rating_low" not in _codes(_run())


# ── 状态跃迁 ────────────────────────────────────────────────────────────────
def test_steady_inactive_listing_is_not_reported(wire):
    """常年「停售」是常态不是事件 —— 只有变成停售才值得惊动人。"""
    wire([_raw(status_text="停售")])
    first = _run()
    assert "listing.deactivated" not in _codes(first)
    wire([_raw(status_text="停售")])
    assert "listing.deactivated" not in _codes(_run())


def test_transition_to_inactive_fires(wire):
    wire([_raw(status_text="在售")])
    _run()
    wire([_raw(status_text="停售")])
    hits = [f for f in _run().findings if f.code == "listing.deactivated"]
    assert len(hits) == 1 and hits[0].severity == "crit"


def test_reactivation_is_info(wire):
    wire([_raw(status_text="停售")])
    _run()
    wire([_raw(status_text="在售")])
    hits = [f for f in _run().findings if f.code == "listing.reactivated"]
    assert len(hits) == 1 and hits[0].severity == "info"


# ── 评分下滑 / 排名 / 价格 ──────────────────────────────────────────────────
def test_rating_drop(wire):
    wire([_raw(stars=4.8, reviews_num=20)])
    _run()
    wire([_raw(stars=4.4, reviews_num=22)])
    hits = [f for f in _run().findings if f.code == "review.rating_drop"]
    assert len(hits) == 1 and hits[0].baseline == 4.8


def test_tiny_rating_change_ignored(wire):
    wire([_raw(stars=4.8, reviews_num=20)])
    _run()
    wire([_raw(stars=4.7, reviews_num=21)])
    assert "review.rating_drop" not in _codes(_run())


def test_rank_worsening_fires_but_improvement_does_not(wire):
    """rank 数值越大越差；变好不该告警。"""
    wire([_raw(seller_rank=100000)])
    _run()
    wire([_raw(seller_rank=150000)])
    assert "rank.drop" in _codes(_run())

    wire([_raw(seller_rank=50000)])
    assert "rank.drop" not in _codes(_run())


def test_price_change_fires(wire):
    wire([_raw(listing_price="35.66")])
    _run()
    wire([_raw(listing_price="25.00")])
    hits = [f for f in _run().findings if f.code == "price.changed_externally"]
    assert len(hits) == 1 and hits[0].baseline == 35.66


def test_small_price_change_ignored(wire):
    wire([_raw(listing_price="35.66")])
    _run()
    wire([_raw(listing_price="36.00")])          # +1%
    assert "price.changed_externally" not in _codes(_run())


# ── 销量 / FBM 库存 ─────────────────────────────────────────────────────────
def test_sales_stall(wire):
    wire([_raw(yesterday_volume="0", average_seven_volume="3.0")])
    hits = [f for f in _run().findings if f.code == "sales.stall"]
    assert len(hits) == 1 and hits[0].baseline == 3.0


def test_dormant_listing_never_reports_stall(wire):
    """账号里大量长尾 listing 近 7 天零销量，对它们报"断单"是噪音。"""
    wire([_raw(yesterday_volume="0", average_seven_volume="0.0") for _ in range(30)])
    assert "sales.stall" not in _codes(_run())


@pytest.mark.parametrize("qty,avg,expected", [
    ("100", "2.0", None),        # 50 天，充足
    ("28", "2.0", "warn"),       # 14 天，恰好等于阈值→不报
    ("26", "2.0", "warn"),       # 13 天
    ("12", "2.0", "crit"),       # 6 天
])
def test_fbm_days_of_cover(wire, qty, avg, expected):
    wire([_raw(quantity=qty, average_seven_volume=avg)])
    hits = [f for f in _run().findings if f.code == "stock.fbm_low"]
    if expected is None:
        assert hits == []
    elif qty == "28":
        assert hits == []        # 边界：等于阈值不报
    else:
        assert len(hits) == 1 and hits[0].severity == expected


def test_dormant_listing_never_reports_low_stock(wire):
    wire([_raw(quantity="1", average_seven_volume="0.0")])
    assert "stock.fbm_low" not in _codes(_run())


# ── 冷启动 ──────────────────────────────────────────────────────────────────
def test_cold_start_suppresses_change_rules(wire):
    wire([_raw(stars=4.0, seller_rank=100000, listing_price="30.00")])
    res = _run()
    assert not [c for c in _codes(res)
                if c in ("review.rating_drop", "rank.drop", "price.changed_externally")]
    assert any("冷启动" in s for s in res.skipped)


# ── 跟卖监控 → Buy Box 风险 ─────────────────────────────────────────────────
class _FollowSrc:
    name, label = "fake_follow", "测试跟卖"

    def __init__(self, rows):
        self.rows = rows

    def supports(self, m):
        return m == "monitor.follow_sale"

    def lag_seconds(self, m):
        return 600.0

    def fetch(self, m, scope, window=None):
        from ivyea_agent.datasources.lingxing_mcp_source import LingxingMcpSource
        return [LingxingMcpSource._follow(r, scope.get("sid")) for r in self.rows]


@pytest.fixture()
def wire_follow(ivyea_home, monkeypatch):
    from ivyea_agent import metrics, datasources

    def _install(rows):
        for s in list(metrics.registered()):
            metrics.unregister(s.name)
        metrics.register(_FollowSrc(rows), priority=1)
        monkeypatch.setattr(datasources, "install_defaults", lambda: None)
    yield _install
    from ivyea_agent import metrics as m
    for s in list(m.registered()):
        m.unregister(s.name)


def _fol(asin="B01", n=1, title="商品"):
    return {"asin": asin, "title": title, "total_seller": n}


def test_new_competitor_is_crit_when_was_alone(wire_follow):
    wire_follow([_fol(n=1)])
    _run()
    wire_follow([_fol(n=2)])
    hits = [f for f in _run().findings if f.code == "buybox.competitor_appeared"]
    assert len(hits) == 1 and hits[0].severity == "crit"


def test_more_competitors_when_already_shared_is_warn(wire_follow):
    wire_follow([_fol(n=2)])
    _run()
    wire_follow([_fol(n=3)])
    hits = [f for f in _run().findings if f.code == "buybox.competitor_appeared"]
    assert len(hits) == 1 and hits[0].severity == "warn"


def test_fewer_competitors_is_not_reported(wire_follow):
    wire_follow([_fol(n=5)])
    _run()
    wire_follow([_fol(n=2)])
    assert "buybox.competitor_appeared" not in _codes(_run())


def test_crowded_fires_without_baseline(wire_follow):
    """拥挤是当前状态，不需要基线也能判。"""
    wire_follow([_fol(n=5)])
    assert "buybox.crowded" in _codes(_run())


def test_no_double_report_for_same_asin(wire_follow):
    """既新增跟卖又拥挤时，只报一条更具体的，不叠加。"""
    wire_follow([_fol(n=3)])
    _run()
    wire_follow([_fol(n=6)])
    codes = _codes(_run())
    assert codes.count("buybox.competitor_appeared") == 1
    assert "buybox.crowded" not in codes


def test_evidence_says_it_is_a_proxy(wire_follow):
    """跟卖数是 Buy Box 竞争的前置信号，不等于已丢失 —— 别让人误读。"""
    wire_follow([_fol(n=5)])
    f = [x for x in _run().findings if x.code == "buybox.crowded"][0]
    assert "不等于已丢失" in str(f.evidence)


def test_no_monitor_configured_is_a_skip_not_a_crash(wire_follow):
    wire_follow([])
    res = _run()
    assert any("跟卖监控" in s for s in res.skipped)
