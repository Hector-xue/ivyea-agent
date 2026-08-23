"""L2 日内层测试：采样差分 + 三条日内规则。"""
from __future__ import annotations

import datetime

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
    # 基线的单位是每小时速率（rate_baseline）：这里 1 单/时，
    # 而本段 1 小时增量 29 单，远超容忍倍数 → 判定为"卖爆了"，不报。
    monkeypatch.setattr(intraday, "rate_baseline",
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


def test_rate_baseline_needs_min_days(ivyea_home):
    from ivyea_agent import intraday

    assert intraday.rate_baseline(1, "campaign", "C1", 10, min_days=3) is None


# ── 基线与采样节奏解耦 ──────────────────────────────────────────────────────
# 巡检节奏从「L2 每小时」改成「每 12 小时」时暴露的问题：老的 hourly_baseline
# 返回的是**两次采样之间的差值**，每小时采样时它恰好等于小时速率，看着没毛病；
# 一改成 12 小时一轮，基线变成 12 小时总量、当前值仍是每小时速率，一比差 12 倍
# —— 规则永远不触发，且不报错。这两条用例把"速率化"钉住。

def _seed_days(sid, entity_id, *, days, gap_hours, spend_per_hour):
    """按给定采样间隔造若干天历史。每天两次采样，间隔 gap_hours。"""
    import time as _t

    from ivyea_agent import intraday
    now = _t.time()
    conn = intraday._conn()
    try:
        for d in range(1, days + 1):
            day = _t.strftime("%Y-%m-%d", _t.localtime(now - d * 86400))
            end_ts = now - d * 86400
            start_ts = end_ts - gap_hours * 3600
            for ts, spend in ((start_ts, 0.0), (end_ts, spend_per_hour * gap_hours)):
                conn.execute(
                    "INSERT INTO samples (sid, entity, entity_id, day, ts, spend, orders)"
                    " VALUES (?,?,?,?,?,?,0)",
                    (str(sid), "campaign", entity_id, day, ts, spend))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("gap_hours", [1, 12])
def test_baseline_is_a_rate_whatever_the_sampling_gap(ivyea_home, gap_hours):
    """同样的"每小时 10 块"，1 小时采一次和 12 小时采一次必须得出同一个基线。"""
    import time as _t

    from ivyea_agent import intraday

    hour = _t.localtime().tm_hour
    _seed_days(1, f"C-{gap_hours}", days=5, gap_hours=gap_hours, spend_per_hour=10.0)
    base = intraday.rate_baseline(1, "campaign", f"C-{gap_hours}", hour, min_days=3)
    assert base is not None
    assert base["spend"] == pytest.approx(10.0, rel=0.01)


def test_baseline_tolerates_the_hour_drifting(ivyea_home):
    """任务按「上次跑完 + 间隔」调度，执行时刻会慢慢漂。
    要求整点严格相等的话，漂过一个小时边界基线就凭空消失。"""
    import time as _t

    from ivyea_agent import intraday

    hour = _t.localtime().tm_hour
    _seed_days(1, "C-drift", days=4, gap_hours=1, spend_per_hour=8.0)
    assert intraday.rate_baseline(1, "campaign", "C-drift",
                                  (hour + 2) % 24, min_days=3) is not None
    # 但差得太远就不该硬凑 —— 凌晨和下午的花费速率本就不是一回事
    assert intraday.rate_baseline(1, "campaign", "C-drift",
                                  (hour + 8) % 24, min_days=3) is None
