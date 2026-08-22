"""L2 日内层测试：采样差分 + 三条日内规则。"""
from __future__ import annotations

import datetime
import time

import pytest


def _today():
    return datetime.date.today().isoformat()


def _yesterday():
    return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


class _Src:
    name = "fake"
    label = "测试源"

    def __init__(self, today_rows, hist_rows, config_rows):
        self.today_rows = today_rows
        self.hist_rows = hist_rows
        self.config_rows = config_rows

    def supports(self, metric):
        return metric in ("ads.campaign_report", "ads.campaign_config")

    def lag_seconds(self, metric):
        return 3600.0

    def fetch(self, metric, scope, window=None):
        if metric == "ads.campaign_config":
            return list(self.config_rows)
        dates = set(window.dates) if window else set()
        if dates == {_today()}:
            return list(self.today_rows)
        return [r for r in self.hist_rows if r["date"] in dates]


@pytest.fixture()
def wire2(ivyea_home, monkeypatch):
    from ivyea_agent import metrics, datasources

    state = {}

    def _install(today_rows=(), hist_rows=(), config_rows=()):
        for s in list(metrics.registered()):
            metrics.unregister(s.name)
        src = _Src(list(today_rows), list(hist_rows), list(config_rows))
        state["src"] = src
        metrics.register(src, priority=1)
        monkeypatch.setattr(datasources, "install_defaults", lambda: None)
        return src
    yield _install
    from ivyea_agent import metrics as m
    for s in list(m.registered()):
        m.unregister(s.name)


def _row(cid="C1", **kw):
    r = {"sid": 1, "date": _today(), "campaign_id": cid, "impressions": 0.0,
         "clicks": 0.0, "spend": 0.0, "orders": 0.0, "sales": 0.0}
    r.update(kw)
    return r


def _hist(cid="C1", days=7, **per_day):
    out = []
    for d in range(1, days + 1):
        day = (datetime.date.today() - datetime.timedelta(days=d)).isoformat()
        row = {"sid": 1, "date": day, "campaign_id": cid, "impressions": 0.0,
               "clicks": 0.0, "spend": 0.0, "orders": 0.0, "sales": 0.0}
        row.update(per_day)
        out.append(row)
    return out


def _conf(cid="C1", name="测试活动", budget=240.0, **kw):
    r = {"sid": 1, "campaign_id": cid, "name": name, "daily_budget": budget,
         "state": "enabled", "serving_status": "ELIGIBLE"}
    r.update(kw)
    return r


def _codes(res):
    return sorted(f.code for f in res.findings)


# ── 采样差分语义 ────────────────────────────────────────────────────────────
def test_first_sample_of_day_reports_no_delta_rule(wire2):
    from ivyea_agent import store_health

    wire2(today_rows=[_row(spend=500.0)], config_rows=[_conf()])
    res = store_health.check_l2(1)
    assert "ads.spend_burst" not in _codes(res)
    assert any("首次采样" in s for s in res.skipped)


def test_negative_delta_treated_as_correction(ivyea_home):
    """亚马逊会回溯修正当日数据。把"花费 -50"当异常报出去是灾难。"""
    from ivyea_agent import intraday

    intraday.record_and_diff(1, "campaign", _today(), [_row(spend=100.0)], "campaign_id")
    r = intraday.record_and_diff(1, "campaign", _today(), [_row(spend=60.0)], "campaign_id")
    assert r.deltas[0].values["spend"] == 0.0
    assert r.deltas[0].corrected is True


def test_cross_day_never_diffed(ivyea_home):
    """新的一天累计值归零；与昨天末次采样相减会得到大负数。"""
    from ivyea_agent import intraday

    intraday.record_and_diff(1, "campaign", _yesterday(), [_row(spend=900.0)], "campaign_id")
    r = intraday.record_and_diff(1, "campaign", _today(), [_row(spend=5.0)], "campaign_id")
    assert r.first_sample_of_day is True
    assert r.deltas == []


def test_short_gap_samples_are_ignored(wire2, monkeypatch):
    """20 分钟内的两次采样，增量全是噪声。"""
    from ivyea_agent import store_health

    wire2(today_rows=[_row(spend=10.0)], config_rows=[_conf()])
    store_health.check_l2(1)
    wire2(today_rows=[_row(spend=500.0)], config_rows=[_conf()])
    assert "ads.spend_burst" not in _codes(store_health.check_l2(1))


# ── 花费突增 ────────────────────────────────────────────────────────────────
def _force_gap(monkeypatch, seconds):
    """把上一次采样的时间戳往前推，制造出足够的采样间隔。"""
    from ivyea_agent import intraday
    conn = intraday._conn()
    try:
        conn.execute("UPDATE samples SET ts = ts - ?", (seconds,))
        conn.commit()
    finally:
        conn.close()


def test_spend_burst_uses_budget_pace_fallback(wire2, monkeypatch):
    """历史样本不足时必须退到日预算配速——否则新用户前三天完全没这条规则，
    而那几天恰恰最容易配错预算烧钱。"""
    from ivyea_agent import store_health

    wire2(today_rows=[_row(spend=10.0)], config_rows=[_conf(budget=240.0)])
    store_health.check_l2(1)                    # 首采
    _force_gap(monkeypatch, 3600)               # 拉开 1 小时
    # 日预算 240 → 配速 10/时；本段 1 小时花 60 → 6 倍 > 2.5 倍
    wire2(today_rows=[_row(spend=70.0)], config_rows=[_conf(budget=240.0)])
    res = store_health.check_l2(1)
    hits = [f for f in res.findings if f.code == "ads.spend_burst"]
    assert len(hits) == 1
    f = hits[0]
    assert "退化基线" in f.evidence["baseline_basis"]
    assert f.action_class == store_health.STANCH
    assert f.intent["change"]["daily_budget"] == pytest.approx(204.0)


def test_spend_burst_silent_when_orders_keep_up(wire2, monkeypatch):
    """花得多但单也多 = 卖爆了，不是烧钱。"""
    from ivyea_agent import store_health, intraday

    wire2(today_rows=[_row(spend=10.0, orders=1.0)], config_rows=[_conf(budget=240.0)])
    store_health.check_l2(1)
    _force_gap(monkeypatch, 3600)
    wire2(today_rows=[_row(spend=70.0, orders=30.0)], config_rows=[_conf(budget=240.0)])
    monkeypatch.setattr(intraday, "hourly_baseline",
                        lambda *a, **k: {"spend": 10.0, "orders": 1.0, "clicks": 5.0,
                                         "impressions": 100.0, "sales": 50.0})
    assert "ads.spend_burst" not in _codes(store_health.check_l2(1))


def test_spend_burst_below_floor_ignored(wire2, monkeypatch):
    from ivyea_agent import store_health

    wire2(today_rows=[_row(spend=1.0)], config_rows=[_conf(budget=24.0)])
    store_health.check_l2(1)
    _force_gap(monkeypatch, 3600)
    wire2(today_rows=[_row(spend=6.0)], config_rows=[_conf(budget=24.0)])   # 增量 5 < 20
    assert "ads.spend_burst" not in _codes(store_health.check_l2(1))


# ── 曝光归零 / 点击无单 ─────────────────────────────────────────────────────
def test_impression_zero(wire2):
    from ivyea_agent import store_health

    wire2(today_rows=[_row(impressions=0.0)],
          hist_rows=_hist(impressions=5000.0),
          config_rows=[_conf()])
    hits = [f for f in store_health.check_l2(1).findings
            if f.code == "ads.impression_zero"]
    assert len(hits) == 1 and hits[0].severity == store_health.CRIT


def test_impression_zero_ignored_for_low_traffic_campaign(wire2):
    from ivyea_agent import store_health

    wire2(today_rows=[_row(impressions=0.0)],
          hist_rows=_hist(impressions=10.0),
          config_rows=[_conf()])
    assert "ads.impression_zero" not in _codes(store_health.check_l2(1))


def test_click_no_order_intraday(wire2):
    from ivyea_agent import store_health

    wire2(today_rows=[_row(clicks=80.0, orders=0.0, spend=90.0)],
          hist_rows=_hist(clicks=20.0, orders=2.0),
          config_rows=[_conf(budget=100.0)])
    hits = [f for f in store_health.check_l2(1).findings
            if f.code == "ads.click_no_order_intraday"]
    assert len(hits) == 1
    assert hits[0].intent["change"]["daily_budget"] == pytest.approx(85.0)


def test_click_no_order_silent_when_orders_exist(wire2):
    from ivyea_agent import store_health

    wire2(today_rows=[_row(clicks=80.0, orders=3.0)],
          hist_rows=_hist(clicks=20.0),
          config_rows=[_conf()])
    assert "ads.click_no_order_intraday" not in _codes(store_health.check_l2(1))


# ── U8 自动观测 ─────────────────────────────────────────────────────────────
def test_no_growth_observed_is_surfaced(wire2, monkeypatch):
    """当日累计值始终不动 = 该源当日数据不滚动，L2 需改由推送承载。
    这条观测让 U8 自己验证自己，不必人工去猜。"""
    from ivyea_agent import store_health

    wire2(today_rows=[_row(spend=10.0)], config_rows=[_conf()])
    store_health.check_l2(1)
    _force_gap(monkeypatch, 3600)
    wire2(today_rows=[_row(spend=10.0)], config_rows=[_conf()])   # 一模一样，没长
    res = store_health.check_l2(1)
    assert any("不滚动更新" in s for s in res.skipped)


def test_hourly_baseline_needs_min_days(ivyea_home):
    from ivyea_agent import intraday

    assert intraday.hourly_baseline(1, "campaign", "C1", 10, min_days=3) is None
