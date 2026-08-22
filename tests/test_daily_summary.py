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
