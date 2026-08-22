"""L1 店铺巡检测试。

fixtures 的字段名与类型**取自 2026-08-22 对领星真实接口的实签调用**，不是臆想的 schema：
- 数值字段混用 int 与字符串（``historical_days_of_supply`` 是 "0.00"）
- ``fba_inventory_level_health_status`` 可能是空串
- FBA 库存接口会把 FBM 商品一并返回（实测某账号 876/876 行为 FBM）
"""
from __future__ import annotations

import pytest


# ── 真实响应形状的 fixtures ─────────────────────────────────────────────────
def _raw_inventory(**over):
    """一行领星 FBA 库存原始响应（字段名/类型照实测）。"""
    row = {
        "sid": 1863, "msku": "TEST-MSKU-1", "asin": "B000TEST01",
        "product_name": "测试商品", "fulfillment_channel_name": "FBA",
        "afn_fulfillable_quantity": 100,
        "afn_inbound_shipped_quantity": 0,
        "afn_inbound_working_quantity": 0,
        "afn_inbound_receiving_quantity": 0,
        "afn_unsellable_quantity": 0,
        "afn_reserved_quantity": 0,
        "historical_days_of_supply": "30.00",      # 字符串型数值
        "sell_through": "0.50",
        "estimated_excess_quantity": "0.00",
        "fba_minimum_inventory_level": "0.00",
        "fba_inventory_level_health_status": "",   # 实测为空串
        "inv_age_365_plus_days": 0,
    }
    row.update(over)
    return row


def _raw_campaign(**over):
    row = {
        "campaign_id": "C1", "name": "测试活动", "state": "enabled",
        "serving_status": "ELIGIBLE", "daily_budget": 100.0,
        "targeting_type": "manual", "last_updated_date": "1653283625066",
    }
    row.update(over)
    return row


class _FakeSource:
    """按指标返回预置行的假数据源，实现 metrics.DataSource 协议。"""
    name = "fake"
    label = "测试源"

    def __init__(self, data):
        self.data = data

    def supports(self, metric):
        return metric in self.data

    def lag_seconds(self, metric):
        return 0.0

    def fetch(self, metric, scope, window=None):
        from ivyea_agent.datasources.lingxing_source import LingxingSource
        raw = self.data[metric]
        sid = scope.get("sid")
        if metric == "inventory.fba_snapshot":
            return [LingxingSource._inventory(r, sid) for r in raw]
        if metric == "ads.campaign_config":
            return [LingxingSource._campaign(r, sid) for r in raw]
        raise AssertionError(metric)


@pytest.fixture()
def wire(ivyea_home, monkeypatch):
    """把假数据源装进指标层，屏蔽真实网络。"""
    from ivyea_agent import metrics, datasources

    def _install(inventory=(), campaigns=()):
        for s in list(metrics.registered()):
            metrics.unregister(s.name)
        metrics.register(_FakeSource({
            "inventory.fba_snapshot": list(inventory),
            "ads.campaign_config": list(campaigns),
        }), priority=1)
        monkeypatch.setattr(datasources, "install_defaults", lambda: None)
    yield _install
    from ivyea_agent import metrics as m
    for s in list(m.registered()):
        m.unregister(s.name)


def _codes(result):
    return sorted(f.code for f in result.findings)


# ── 规范化 ──────────────────────────────────────────────────────────────────
def test_string_numbers_are_normalized(ivyea_home):
    from ivyea_agent.datasources.lingxing_source import LingxingSource

    row = LingxingSource._inventory(_raw_inventory(), 1863)
    assert row["days_of_supply"] == 30.0        # "30.00" → float
    assert isinstance(row["days_of_supply"], float)
    assert row["health_status"] == ""            # 空串保持空，不得变成 "NONE"
    assert row["channel"] == "FBA"


# ── 库存规则 ────────────────────────────────────────────────────────────────
def test_fbm_rows_never_trigger_stock_rules(wire):
    """实测教训：FBA 接口会返回 FBM 商品，其库存恒为 0。
    不过滤 channel 就会对每一个自发货商品误报断货。"""
    from ivyea_agent import store_health

    fbm = [_raw_inventory(msku=f"M{i}", fulfillment_channel_name="FBM",
                          afn_fulfillable_quantity=0,
                          historical_days_of_supply="0.00") for i in range(50)]
    wire(inventory=fbm)
    res = store_health.check_l1(1)
    assert res.findings == []
    assert any("不是 FBA 渠道" in s for s in res.skipped)


def test_out_of_stock_detected(wire):
    from ivyea_agent import store_health

    wire(inventory=[_raw_inventory(afn_fulfillable_quantity=0,
                                   historical_days_of_supply="0.00")])
    res = store_health.check_l1(1)
    assert "stock.oos" in _codes(res)
    f = [x for x in res.findings if x.code == "stock.oos"][0]
    assert f.severity == store_health.CRIT
    assert f.evidence["fulfillable"] == 0.0


def test_out_of_stock_not_reported_when_inbound_exists(wire):
    """有在途就不是断货——补货在路上，报 crit 是噪音。"""
    from ivyea_agent import store_health

    wire(inventory=[_raw_inventory(afn_fulfillable_quantity=0,
                                   afn_inbound_shipped_quantity=200,
                                   historical_days_of_supply="0.00")])
    assert "stock.oos" not in _codes(store_health.check_l1(1))


@pytest.mark.parametrize("dos,expected", [
    ("30.00", None),                      # 高于阈值：不报
    ("14.00", None),                      # 恰好等于阈值：不报（边界）
    ("13.90", "warn"),                    # 刚跌破：warn
    ("7.00", "warn"),                     # 恰好等于 crit 阈值：仍 warn（边界）
    ("6.90", "crit"),                     # 跌破 crit
])
def test_days_low_boundaries(wire, dos, expected):
    from ivyea_agent import store_health

    wire(inventory=[_raw_inventory(historical_days_of_supply=dos)])
    res = store_health.check_l1(1)
    hits = [f for f in res.findings if f.code == "stock.days_low"]
    if expected is None:
        assert hits == []
    else:
        assert len(hits) == 1 and hits[0].severity == expected


def test_empty_health_status_never_alerts(wire):
    """实测该字段可能是空串；空值报警会让每个商品都出一条噪音。"""
    from ivyea_agent import store_health

    wire(inventory=[_raw_inventory(fba_inventory_level_health_status="")])
    assert "stock.health_bad" not in _codes(store_health.check_l1(1))


def test_bad_health_status_alerts(wire):
    from ivyea_agent import store_health

    wire(inventory=[_raw_inventory(fba_inventory_level_health_status="Excess")])
    assert "stock.health_bad" in _codes(store_health.check_l1(1))


def test_excess_inventory(wire):
    from ivyea_agent import store_health

    wire(inventory=[_raw_inventory(estimated_excess_quantity="42.00")])
    hits = [f for f in store_health.check_l1(1).findings if f.code == "stock.excess"]
    assert len(hits) == 1 and hits[0].current == 42.0


def test_unsellable_spike_needs_baseline_then_fires(wire):
    from ivyea_agent import store_health

    wire(inventory=[_raw_inventory(afn_unsellable_quantity=10)])
    first = store_health.check_l1(1)
    assert "stock.unsellable_spike" not in _codes(first)
    assert any("冷启动" in s for s in first.skipped)

    wire(inventory=[_raw_inventory(afn_unsellable_quantity=20)])
    second = store_health.check_l1(1)
    hits = [f for f in second.findings if f.code == "stock.unsellable_spike"]
    assert len(hits) == 1
    assert hits[0].baseline == 10.0 and hits[0].current == 20.0


def test_unsellable_spike_ignores_tiny_base(wire):
    """1 → 2 是 +100%，但绝对量太小，报了就是噪音。"""
    from ivyea_agent import store_health

    wire(inventory=[_raw_inventory(afn_unsellable_quantity=1)])
    store_health.check_l1(1)
    wire(inventory=[_raw_inventory(afn_unsellable_quantity=2)])
    assert "stock.unsellable_spike" not in _codes(store_health.check_l1(1))


# ── 广告活动规则 ────────────────────────────────────────────────────────────
def test_out_of_budget_produces_executable_intent(wire):
    from ivyea_agent import store_health

    wire(campaigns=[_raw_campaign(serving_status="CAMPAIGN_OUT_OF_BUDGET",
                                  daily_budget=100.0)])
    res = store_health.check_l1(1)
    hits = [f for f in res.findings if f.code == "ads.campaign_out_of_budget"]
    assert len(hits) == 1
    f = hits[0]
    assert f.action_class == store_health.STANCH
    assert f.executable
    assert f.intent["op_type"] == "campaign_budget"
    # 止血幅度封顶 15%
    assert f.intent["change"]["daily_budget"] == pytest.approx(115.0)
    assert f.intent["before"]["daily_budget"] == 100.0


def test_stanch_intent_passes_write_magnitude_gate(wire):
    """止血建议必须能过 lingxing_write 的幅度硬闸，否则点了批准也会被拦。"""
    from ivyea_agent import store_health, lingxing_write

    wire(campaigns=[_raw_campaign(serving_status="CAMPAIGN_OUT_OF_BUDGET",
                                  daily_budget=100.0)])
    f = [x for x in store_health.check_l1(1).findings
         if x.code == "ads.campaign_out_of_budget"][0]
    ok, why = lingxing_write.magnitude_ok(f.intent)
    assert ok, why


def test_campaign_pause_and_budget_change_need_baseline(wire):
    from ivyea_agent import store_health

    wire(campaigns=[_raw_campaign(state="enabled", daily_budget=100.0)])
    first = store_health.check_l1(1)
    assert not [f for f in first.findings if f.code.startswith("ads.campaign_unexpected")]
    assert any("冷启动" in s for s in first.skipped)

    wire(campaigns=[_raw_campaign(state="paused", daily_budget=30.0)])
    second = store_health.check_l1(1)
    codes = _codes(second)
    assert "ads.campaign_unexpected_pause" in codes
    assert "ads.budget_changed_externally" in codes


def test_small_budget_change_is_ignored(wire):
    from ivyea_agent import store_health

    wire(campaigns=[_raw_campaign(daily_budget=100.0)])
    store_health.check_l1(1)
    wire(campaigns=[_raw_campaign(daily_budget=102.0)])   # +2% < 5% 阈值
    assert "ads.budget_changed_externally" not in _codes(store_health.check_l1(1))


def test_own_write_suppresses_external_change_alert(wire, monkeypatch):
    """agent 自己刚改过的预算，不该反过来告警说"被外部改动"。"""
    from ivyea_agent import store_health, audit

    wire(campaigns=[_raw_campaign(daily_budget=100.0)])
    store_health.check_l1(1)
    audit.record({"target_id": "C1", "kind": "campaign_budget"})
    wire(campaigns=[_raw_campaign(daily_budget=50.0)])
    assert "ads.budget_changed_externally" not in _codes(store_health.check_l1(1))


# ── 数据缺口不得被静默吞掉 ──────────────────────────────────────────────────
def test_missing_source_surfaces_as_gap(ivyea_home, monkeypatch):
    from ivyea_agent import metrics, store_health, datasources

    for s in list(metrics.registered()):
        metrics.unregister(s.name)
    monkeypatch.setattr(datasources, "install_defaults", lambda: None)
    res = store_health.check_l1(1)
    # 不断言具体条数 —— 每加一个指标就要改一次数字，那是测试自身的坏味道。
    # 要保证的是：每个查不到的指标都**各自**报了缺口，而不是被吞掉。
    assert res.findings == []
    assert res.gaps, "没有数据源时必须报缺口，不能静默返回「无异常」"
    assert all("无数据" in g for g in res.gaps)
    assert "数据缺口" in store_health.render(res)
