"""L3 隔日层测试。

账号当前无活跃广告（实测活动全部 ADVERTISER_ARCHIVED），因此这一层用 fixtures 驱动。
fixtures 的字段名取自领星真实响应结构（见 lingxing_source 的规范化映射）。
一旦有在投账号，同一批规则直接跑真实数据即可补验收——规则本身不因缺数据而缩水。
"""
from __future__ import annotations

import datetime

import pytest


def _dates(days=7, excl=1):
    from ivyea_agent import store_health
    return store_health._window_days(days, excl)


def _report_rows(campaign_id, dates, *, clicks, spend, orders, sales, impressions=1000):
    """按天均摊一组指标，构造报表行（canonical 字段）。"""
    n = len(dates)
    return [{"sid": 1, "date": d, "campaign_id": campaign_id,
             "impressions": impressions / n, "clicks": clicks / n,
             "spend": spend / n, "orders": orders / n, "sales": sales / n}
            for d in dates]


class _Src:
    name = "fake"
    label = "测试源"

    def __init__(self, data):
        self.data = data

    def supports(self, metric):
        return metric in self.data

    def lag_seconds(self, metric):
        return 86400.0

    def fetch(self, metric, scope, window=None):
        val = self.data[metric]
        if callable(val):
            return val(scope, window)
        return list(val)


@pytest.fixture()
def wire3(ivyea_home, monkeypatch):
    from ivyea_agent import metrics, datasources, lingxing_optimizer

    def _install(report=(), config_rows=(), profit_by_window=None):
        for s in list(metrics.registered()):
            metrics.unregister(s.name)

        def _profit(scope, window):
            if not profit_by_window:
                return []
            return list(profit_by_window.get(tuple(window.dates) if window else (), []))

        metrics.register(_Src({
            "ads.campaign_report": list(report),
            "ads.campaign_config": list(config_rows),
            "profit.asin": _profit,
        }), priority=1)
        monkeypatch.setattr(datasources, "install_defaults", lambda: None)
        monkeypatch.setattr(lingxing_optimizer, "resolve_target_acos",
                            lambda sid: (0.30, 0.40, 0.40, "测试目标"))
    yield _install
    from ivyea_agent import metrics as m
    for s in list(m.registered()):
        m.unregister(s.name)


def _run(**kw):
    from ivyea_agent import store_health
    return store_health.check_l3(1, include_optimizer=False, **kw)


def _codes(res):
    return sorted(f.code for f in res.findings)


# ── 窗口 ────────────────────────────────────────────────────────────────────
def test_window_excludes_today_and_does_not_overlap(ivyea_home):
    """报表 T+1：拿今天的半天数据和整天基线比，会造出"销量腰斩"的假告警。"""
    recent, base = _dates(7)
    today = datetime.date.today().isoformat()
    assert today not in recent and today not in base
    assert set(recent).isdisjoint(base)
    assert len(recent) == len(base) == 7
    assert max(base) < min(recent)


# ── ACOS 超标 ───────────────────────────────────────────────────────────────
def test_acos_breach_fires_with_executable_intent(wire3):
    recent, base = _dates()
    wire3(report=_report_rows("C1", recent, clicks=500, spend=900, orders=10, sales=1000),
          config_rows=[{"sid": 1, "campaign_id": "C1", "name": "高ACOS活动",
                        "daily_budget": 100.0, "state": "enabled",
                        "serving_status": "ELIGIBLE"}])
    res = _run()
    hits = [f for f in res.findings if f.code == "ads.acos_breach"]
    assert len(hits) == 1
    f = hits[0]
    assert f.current == pytest.approx(0.9)          # ACOS 90% > 30% × 1.5
    assert f.executable and f.intent["change"]["daily_budget"] == pytest.approx(85.0)


def test_acos_breach_ignored_below_spend_floor(wire3):
    """小花费的高 ACOS 是噪音，不值得推送。"""
    recent, _ = _dates()
    wire3(report=_report_rows("C1", recent, clicks=20, spend=50, orders=1, sales=55),
          config_rows=[{"sid": 1, "campaign_id": "C1", "name": "小花费",
                        "daily_budget": 20.0}])
    assert "ads.acos_breach" not in _codes(_run())


def test_acos_breach_without_budget_is_advisory_only(wire3):
    """拿不到日预算就构造不出可执行 intent，此时只报不建议执行。"""
    recent, _ = _dates()
    wire3(report=_report_rows("C1", recent, clicks=500, spend=900, orders=10, sales=1000),
          config_rows=[])
    hits = [f for f in _run().findings if f.code == "ads.acos_breach"]
    assert len(hits) == 1 and not hits[0].executable


# ── CPC 跳涨 ────────────────────────────────────────────────────────────────
def test_cpc_jump_requires_worse_conversion(wire3):
    """CPC 涨但转化同步变好 = 竞价买到了更好的流量，不该告警。"""
    recent, base = _dates()
    rows = (_report_rows("C1", base, clicks=100, spend=100, orders=5, sales=200)
            + _report_rows("C1", recent, clicks=100, spend=200, orders=20, sales=800))
    wire3(report=rows, config_rows=[{"sid": 1, "campaign_id": "C1", "name": "C1",
                                     "daily_budget": 500.0}])
    assert "ads.cpc_jump" not in _codes(_run())


def test_cpc_jump_fires_when_conversion_flat(wire3):
    recent, base = _dates()
    rows = (_report_rows("C1", base, clicks=100, spend=100, orders=5, sales=300)
            + _report_rows("C1", recent, clicks=100, spend=200, orders=5, sales=300))
    wire3(report=rows, config_rows=[{"sid": 1, "campaign_id": "C1", "name": "C1",
                                     "daily_budget": 500.0}])
    hits = [f for f in _run().findings if f.code == "ads.cpc_jump"]
    assert len(hits) == 1
    assert hits[0].action_class == "advisory"   # 杠杆在关键词 bid，交给优化器


def test_cpc_jump_ignored_when_clicks_too_few(wire3):
    recent, base = _dates()
    rows = (_report_rows("C1", base, clicks=10, spend=10, orders=1, sales=50)
            + _report_rows("C1", recent, clicks=10, spend=20, orders=1, sales=50))
    wire3(report=rows, config_rows=[{"sid": 1, "campaign_id": "C1", "name": "C1",
                                     "daily_budget": 500.0}])
    assert "ads.cpc_jump" not in _codes(_run())


# ── 预算打满 ────────────────────────────────────────────────────────────────
def test_budget_capped_suggests_raise_when_healthy(wire3):
    recent, _ = _dates()
    # 7 天花 700 → 日均 100，预算 100 打满；ACOS 10% 优于目标 30%
    wire3(report=_report_rows("C1", recent, clicks=300, spend=700, orders=50, sales=7000),
          config_rows=[{"sid": 1, "campaign_id": "C1", "name": "好活动",
                        "daily_budget": 100.0}])
    hits = [f for f in _run().findings if f.code == "ads.budget_capped"]
    assert len(hits) == 1
    assert hits[0].intent["change"]["daily_budget"] == pytest.approx(115.0)


def test_budget_capped_not_raised_when_acos_bad(wire3):
    """打满但 ACOS 超标，提预算等于加速烧钱。"""
    recent, _ = _dates()
    wire3(report=_report_rows("C1", recent, clicks=300, spend=700, orders=5, sales=800),
          config_rows=[{"sid": 1, "campaign_id": "C1", "name": "差活动",
                        "daily_budget": 100.0}])
    assert "ads.budget_capped" not in _codes(_run())


# ── 销量 / 毛利 ─────────────────────────────────────────────────────────────
def test_sales_drop_and_margin_erosion(wire3):
    recent, base = _dates()
    wire3(profit_by_window={
        tuple(recent): [{"sid": 1, "asin": "B01", "sales_amount": 300.0,
                         "gross_rate": 0.10, "gross_profit": 30.0, "ads_cost": 50.0}],
        tuple(base): [{"sid": 1, "asin": "B01", "sales_amount": 1000.0,
                       "gross_rate": 0.25, "gross_profit": 250.0, "ads_cost": 50.0}],
    })
    codes = _codes(_run())
    assert "sales.drop" in codes and "profit.margin_erosion" in codes


def test_sales_drop_ignores_small_baseline(wire3):
    recent, base = _dates()
    wire3(profit_by_window={
        tuple(recent): [{"sid": 1, "asin": "B01", "sales_amount": 5.0, "gross_rate": 0.2}],
        tuple(base): [{"sid": 1, "asin": "B01", "sales_amount": 50.0, "gross_rate": 0.2}],
    })
    assert "sales.drop" not in _codes(_run())


def test_gross_rate_percentage_form_is_normalized(wire3):
    """领星毛利率可能以 23.5 表示 23.5%；不折算会把 23.5→10.0 当成下降 13.5 个点的误报……
    反之亦然。两边同为百分数时必须折算后再比。"""
    recent, base = _dates()
    wire3(profit_by_window={
        tuple(recent): [{"sid": 1, "asin": "B01", "sales_amount": 1000.0, "gross_rate": 24.0}],
        tuple(base): [{"sid": 1, "asin": "B01", "sales_amount": 1000.0, "gross_rate": 25.0}],
    })
    # 25% → 24%，仅降 1 个百分点，低于 5pp 阈值
    assert "profit.margin_erosion" not in _codes(_run())


# ── 数据缺口 ────────────────────────────────────────────────────────────────
def test_missing_report_surfaces_gap_not_silence(wire3):
    from ivyea_agent import metrics

    wire3()
    for s in list(metrics.registered()):
        metrics.unregister(s.name)
    res = _run()
    assert res.findings == []
    assert res.gaps, "没有数据源时必须报数据缺口，不能静默返回'无异常'"


# ── 与优化器的分工 ──────────────────────────────────────────────────────────
def test_optimizer_candidates_become_findings(wire3, monkeypatch):
    from ivyea_agent import store_health, lingxing_optimizer

    wire3()
    monkeypatch.setattr(lingxing_optimizer, "run_store", lambda sid: {
        "window_days": 30, "note": "测试",
        "candidates": [
            {"lever": "否词", "op_type": "negate_keyword", "sid": 1, "campaign_id": "C1",
             "target_name": "bad term", "metrics": {"clicks": 20}, "blocked": False,
             "rule": "20点击0单 → 否定"},
            {"lever": "降bid", "op_type": "keyword_bid", "sid": 1, "target_id": "K1",
             "target_name": "kw", "metrics": {}, "blocked": False, "rule": "降bid",
             "current": {"bid": 1.0}, "proposed": {"bid": 0.85}},
            {"lever": "否词", "op_type": "negate_keyword", "sid": 1, "target_name": "x",
             "metrics": {}, "blocked": True, "block_reason": "冷却期内", "rule": ""},
        ]})
    res = store_health.check_l3(1, include_optimizer=True)
    codes = _codes(res)
    assert "ads.opt.negate_keyword" in codes and "ads.opt.keyword_bid" in codes
    neg = [f for f in res.findings if f.code == "ads.opt.negate_keyword"][0]
    bid = [f for f in res.findings if f.code == "ads.opt.keyword_bid"][0]
    # 否词不可逆 → 结构型；调价可逆 → 止血型（ADR-7）
    assert neg.action_class == store_health.STRUCTURAL
    assert bid.action_class == store_health.STANCH
    assert neg.executable and bid.executable
    # 被冷却/护栏拦截的不进建议，但要让人看见
    assert any("拦截" in s for s in res.skipped)


def test_optimizer_failure_is_a_gap_not_a_crash(wire3, monkeypatch):
    from ivyea_agent import store_health, lingxing_optimizer

    wire3()

    def _boom(sid):
        raise RuntimeError("领星超时")

    monkeypatch.setattr(lingxing_optimizer, "run_store", _boom)
    res = store_health.check_l3(1, include_optimizer=True)
    assert any("优化器候选不可用" in g for g in res.gaps)
