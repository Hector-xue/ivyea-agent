"""指标层（ADR-8）测试：规则只认 canonical 字段，换数据源不改规则。"""
from __future__ import annotations

import pytest


class _Src:
    def __init__(self, name, metrics_map, lag=0.0, boom=False):
        self.name = name
        self.label = f"源{name}"
        self._map = metrics_map
        self._lag = lag
        self._boom = boom
        self.calls = 0

    def supports(self, metric):
        return metric in self._map

    def lag_seconds(self, metric):
        return self._lag

    def fetch(self, metric, scope, window=None):
        self.calls += 1
        if self._boom:
            raise RuntimeError("源炸了")
        return list(self._map[metric])


@pytest.fixture()
def clean(ivyea_home):
    from ivyea_agent import metrics
    for s in list(metrics.registered()):
        metrics.unregister(s.name)
    yield metrics
    for s in list(metrics.registered()):
        metrics.unregister(s.name)


def test_registry_covers_expected_metrics(clean):
    assert "inventory.fba_snapshot" in clean.REGISTRY
    assert clean.REGISTRY["inventory.fba_snapshot"].grain == clean.GRAIN_SNAPSHOT
    assert clean.REGISTRY["ads.campaign_report"].grain == clean.GRAIN_DAILY


def test_unknown_metric_returns_gap_not_exception(clean):
    r = clean.get_metric("nope.nope")
    assert not r.ok and "未注册" in r.gap.describe()


def test_no_source_returns_gap(clean):
    r = clean.get_metric("inventory.fba_snapshot", {"sid": 1})
    assert not r.ok and "没有任何已注册数据源" in r.gap.describe()


def test_priority_order_decides_winner(clean):
    """推送源优先级更高，即使轮询源也能给出同一指标。"""
    push = _Src("push", {"inventory.fba_snapshot": [{"msku": "A"}]}, lag=5.0)
    poll = _Src("poll", {"inventory.fba_snapshot": [{"msku": "B"}]}, lag=86400.0)
    clean.register(poll, priority=100)
    clean.register(push, priority=10)

    r = clean.get_metric("inventory.fba_snapshot", {"sid": 1})
    assert r.ok and r.rows[0]["msku"] == "A"
    assert r.provenance.source == "push"
    assert poll.calls == 0


def test_failing_source_falls_through_to_next(clean):
    bad = _Src("bad", {"inventory.fba_snapshot": []}, boom=True)
    good = _Src("good", {"inventory.fba_snapshot": [{"msku": "C"}]})
    clean.register(bad, priority=10)
    clean.register(good, priority=20)

    r = clean.get_metric("inventory.fba_snapshot", {"sid": 1})
    assert r.ok and r.provenance.source == "good"


def test_all_sources_failing_reports_reasons(clean):
    clean.register(_Src("bad", {"inventory.fba_snapshot": []}, boom=True), priority=10)
    r = clean.get_metric("inventory.fba_snapshot", {"sid": 1})
    assert not r.ok and "源炸了" in r.gap.reason


def test_re_register_replaces_not_duplicates(clean):
    clean.register(_Src("x", {"inventory.fba_snapshot": [{"msku": "1"}]}), priority=50)
    clean.register(_Src("x", {"inventory.fba_snapshot": [{"msku": "2"}]}), priority=50)
    assert len(clean.registered()) == 1
    assert clean.get_metric("inventory.fba_snapshot", {"sid": 1}).rows[0]["msku"] == "2"


@pytest.mark.parametrize("lag,expect", [
    (5.0, "实时"), (600.0, "延迟约 10 分钟"),
    (7200.0, "延迟约 2 小时"), (86400.0 * 2, "延迟约 2 天"),
])
def test_provenance_describes_lag_in_human_terms(clean, lag, expect):
    clean.register(_Src("s", {"inventory.fba_snapshot": [{"msku": "A"}]}, lag=lag))
    r = clean.get_metric("inventory.fba_snapshot", {"sid": 1})
    assert expect in r.provenance.describe()


@pytest.mark.parametrize("raw,expected", [
    ("0.00", 0.0), ("30.00", 30.0), (12, 12.0), ("1,234.5", 1234.5),
    ("", 0.0), (None, 0.0), ("abc", 0.0), ("15%", 15.0),
])
def test_num_handles_lingxing_mixed_types(clean, raw, expected):
    assert clean.num(raw) == expected
