"""早报汇总测试（方案 §5.5）。"""
from __future__ import annotations

import datetime

import pytest


def _dates(days=1, excl=1):
    from ivyea_agent import store_health
    return store_health._window_days(days, excl)


class _Src:
    name = "fake"
    label = "测试源"

    def __init__(self, data):
        self.data = data

    def supports(self, metric):
        return metric in self.data

    def lag_seconds(self, metric):
        return 0.0

    def fetch(self, metric, scope, window=None):
        v = self.data[metric]
        return v(window) if callable(v) else list(v)


@pytest.fixture()
def wire(ivyea_home, monkeypatch):
    from ivyea_agent import metrics, datasources

    def _install(**data):
        for s in list(metrics.registered()):
            metrics.unregister(s.name)
        metrics.register(_Src(data), priority=1)
        monkeypatch.setattr(datasources, "install_defaults", lambda: None)
    yield _install
    from ivyea_agent import metrics as m
    for s in list(m.registered()):
        m.unregister(s.name)


def _rep(day, cid="C1", **kw):
    r = {"sid": 1, "date": day, "campaign_id": cid, "impressions": 0.0,
         "clicks": 0.0, "spend": 0.0, "orders": 0.0, "sales": 0.0}
    r.update(kw)
    return r


def test_summary_computes_deltas(wire):
    from ivyea_agent import store_health

    recent, base = _dates()
    wire(**{"ads.campaign_report":
            [_rep(base[0], spend=100.0, sales=500.0, orders=10.0, clicks=50.0),
             _rep(recent[0], spend=120.0, sales=400.0, orders=8.0, clicks=60.0)]})
    d = store_health.daily_summary(1)
    text = "\n".join(d["lines"])
    assert "花费 120.00" in text and "▲20%" in text     # 100 → 120
    assert "▼20%" in text                                # 销售额 500 → 400
    m = d["metrics"]
    assert m["acos"] == pytest.approx(0.3)               # 120/400
    # ACOS 20% → 30%，用百分点表示而不是百分比，避免"涨了50%"的误读
    assert "pp" in text


def test_no_baseline_says_so_rather_than_faking_zero(wire):
    from ivyea_agent import store_health

    recent, _base = _dates()
    wire(**{"ads.campaign_report": [_rep(recent[0], spend=10.0, sales=50.0)]})
    assert "无对照" in "\n".join(store_health.daily_summary(1)["lines"])


def test_missing_data_becomes_gap_not_zero(wire):
    """空账号上报「无数据」，不能编出一行全 0 的漂亮指标。"""
    from ivyea_agent import store_health

    wire(**{"ads.campaign_report": []})
    d = store_health.daily_summary(1)
    assert d["lines"] == []
    assert any("无广告报表数据" in g for g in d["gaps"])


def test_inventory_line_counts_oos_and_low(wire):
    from ivyea_agent import store_health
    from ivyea_agent.datasources.lingxing_source import LingxingSource

    def _inv(_w):
        raw = [
            {"msku": "A", "fulfillment_channel_name": "FBA",
             "afn_fulfillable_quantity": 0, "historical_days_of_supply": "0.00"},
            {"msku": "B", "fulfillment_channel_name": "FBA",
             "afn_fulfillable_quantity": 5, "historical_days_of_supply": "3.00"},
            {"msku": "C", "fulfillment_channel_name": "FBA",
             "afn_fulfillable_quantity": 99, "historical_days_of_supply": "60.00"},
            {"msku": "D", "fulfillment_channel_name": "FBM",
             "afn_fulfillable_quantity": 0, "historical_days_of_supply": "0.00"},
        ]
        return [LingxingSource._inventory(r, 1) for r in raw]

    wire(**{"inventory.fba_snapshot": _inv})
    d = store_health.daily_summary(1)
    m = d["metrics"]
    assert m["fba_skus"] == 3 and m["oos"] == 1 and m["low"] == 1   # FBM 不计入


def test_all_fbm_reports_gap(wire):
    from ivyea_agent import store_health
    from ivyea_agent.datasources.lingxing_source import LingxingSource

    wire(**{"inventory.fba_snapshot": lambda _w: [
        LingxingSource._inventory({"msku": "D", "fulfillment_channel_name": "FBM"}, 1)]})
    d = store_health.daily_summary(1)
    assert any("非 FBA 渠道" in g for g in d["gaps"])


def test_window_excludes_today(ivyea_home):
    from ivyea_agent import store_health

    recent, base = store_health._window_days(1)
    assert datetime.date.today().isoformat() not in recent + base
    assert set(recent).isdisjoint(base)


# ── schedule 装配 ───────────────────────────────────────────────────────────
def test_daily_task_pushes_card(ivyea_home, monkeypatch):
    from ivyea_agent import schedule, store_health, patrol_push

    monkeypatch.setattr(store_health, "check_l3",
                        lambda sid, **kw: store_health.CheckResult(sid=sid, layer="L3"))
    monkeypatch.setattr(store_health, "daily_summary",
                        lambda sid, **kw: {"lines": ["销售额 100"], "metrics": {},
                                           "gaps": ["利润无数据"]})
    seen = {}

    def _push(result, **kw):
        seen.update(kw)
        seen["gaps"] = list(result.gaps)
        return {"ok": True, "message_id": "om_1", "approvals": []}

    monkeypatch.setattr(patrol_push, "push_daily", _push)
    ok, text = schedule.run_task("store_daily", {"sid": 1, "channel": "feishu_app",
                                                 "store_name": "UK"})
    assert ok and "早报已推送" in text
    assert seen["metrics_lines"] == ["销售额 100"]
    assert "利润无数据" in seen["gaps"], "数据缺口必须进卡片，不能只留在日志"


def test_daily_task_reports_push_failure(ivyea_home, monkeypatch):
    from ivyea_agent import schedule, store_health, patrol_push

    monkeypatch.setattr(store_health, "check_l3",
                        lambda sid, **kw: store_health.CheckResult(sid=sid, layer="L3"))
    monkeypatch.setattr(store_health, "daily_summary",
                        lambda sid, **kw: {"lines": [], "metrics": {}, "gaps": []})
    monkeypatch.setattr(patrol_push, "push_daily",
                        lambda *a, **k: {"ok": False, "error": "bot not in chat"})
    ok, text = schedule.run_task("store_daily", {"sid": 1, "channel": "feishu_app"})
    assert not ok and "bot not in chat" in text


def test_daily_task_falls_back_to_text_channel(ivyea_home, monkeypatch):
    """非 feishu_app 通道走原有纯文本路径，不该被卡片装配劫持。"""
    from ivyea_agent import schedule, store_health, notify

    monkeypatch.setattr(store_health, "check_l3",
                        lambda sid, **kw: store_health.CheckResult(sid=sid, layer="L3"))
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(k) or {"ok": True})
    ok, _t = schedule.run_task("store_daily", {"sid": 1, "notify": True,
                                               "channel": "stdout"})
    assert ok and len(sent) == 1


# ── Listing 维度（领星 MCP 源）──────────────────────────────────────────────
def _listing_rows(n=3, **over):
    from ivyea_agent.datasources.lingxing_mcp_source import LingxingMcpSource

    base = {"store_id": 1, "msku": "M", "asin": "B", "item_name": "商品",
            "fulfillment_channel_type": "FBM", "status_text": "在售",
            "listing_price": "30.00", "stars": 4.5, "reviews_num": 10,
            "seller_rank": 1000, "quantity": "50",
            "yesterday_volume": "2", "seven_volume": "14", "thirty_volume": "30",
            "average_seven_volume": "2.0", "average_thirty_volume": "1.0",
            "seven_amount": "420.00", "thirty_amount": "900.00",
            "seven_spend": "42.00", "thirty_spend": "90.00"}
    base.update(over)
    return [LingxingMcpSource._listing(dict(base, msku=f"M{i}"), 1) for i in range(n)]


def test_daily_includes_listing_dimension(wire):
    """这个账号广告和利润都是空的，但 listing 维度有真实销量 ——
    早报不能因此变成一张只有"无数据"的卡。"""
    from ivyea_agent import store_health

    wire(**{"listing.snapshot": lambda _w: _listing_rows(3)})
    d = store_health.daily_summary(1)
    text = "\n".join(d["lines"])
    assert "销量" in text and "昨日 6 件" in text        # 3 条 × 2
    assert "Listing" in text and "120" not in text
    assert d["metrics"]["listings"] == 3 and d["metrics"]["on_sale"] == 3
    assert d["metrics"]["ad_spend_7"] == pytest.approx(126.0)


def test_daily_counts_low_rated_listings(wire):
    from ivyea_agent import store_health

    rows = _listing_rows(2) + _listing_rows(1, stars=2.0, reviews_num=8)
    wire(**{"listing.snapshot": lambda _w: rows})
    assert "评分偏低 1" in "\n".join(store_health.daily_summary(1)["lines"])


def test_daily_trend_is_labelled_as_7d_vs_30d(wire):
    """领星按 listing 只给 7 日/30 日窗口，没有"昨日 vs 前日"。
    比的是趋势不是日环比，卡片上必须标清楚，别让人误读。"""
    from ivyea_agent import store_health

    wire(**{"listing.snapshot": lambda _w: _listing_rows(1)})
    text = "\n".join(store_health.daily_summary(1)["lines"])
    assert "30 日均" in text


def test_daily_reports_gap_when_no_listing_source(wire):
    from ivyea_agent import store_health

    wire()
    d = store_health.daily_summary(1)
    assert any("listing.snapshot" in g or "Listing" in g for g in d["gaps"])


def test_check_l1_is_unaffected_by_summary_code(wire):
    """回归：早报的 listing 汇总代码一度被误插进 check_l1，
    那里没有 lines/out/gaps，会直接 NameError。"""
    from ivyea_agent import store_health

    wire(**{"listing.snapshot": lambda _w: _listing_rows(2)})
    res = store_health.check_l1(1)
    assert isinstance(res, store_health.CheckResult)
    assert res.layer == "L1"
