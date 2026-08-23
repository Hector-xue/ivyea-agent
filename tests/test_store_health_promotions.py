"""L1 促销规则：已报活动 / 优惠券的临期与预算告警。

fixtures 的字段名取自领星官方接口文档并经本机实签调用核实（该账号当前没有促销
数据 —— 四个接口 code=0、total=0 —— 所以真数据验收要等有在跑活动的账号；
契约与判定逻辑先在这里钉死）。

这几条规则最容易错的地方，逐条守住：

* **时区**：领星给的是站点当地时间的裸字符串。按服务器时区解释，UK 的活动
  会差 7~8 小时 —— 正好是"以为还有一天、其实已经结束"这种最坏的错法。
* **档位**：24 小时 / 6 小时两档，每档只报一次。每小时都报的机器人会被静音，
  那时候真到期也没人看。
* **已取消/已过期的活动没有"还剩多久"**，不能混进倒计时。
* **数据停更要单独报**：插件掉线时接口照样 200 + 旧数据，倒计时会安静地停住。
"""
from __future__ import annotations

import datetime as dt

import pytest


def _raw_promo(**over):
    """一行领星促销活动原始响应（字段名照官方文档）。"""
    row = {
        "promotion_id": "P-1", "name": "Save £2", "sid": 1863,
        "currency_icon": "£", "origin_status": "ACTIVE",
        "discount": "£2.00", "budget": "£1000.00", "cost": "100.00",
        "draw_quantity": "12", "exchange_quantity": "3", "exchange_rate": "25",
        "sales_amount": "240.00", "sales_volume": "9",
        "promotion_start_time": "", "promotion_end_time": "",
        "first_sync_time": "", "last_sync_time": "", "remark": "",
    }
    row.update(over)
    return row


def _site_time(hours_from_now: float, tz_name: str = "Europe/London") -> str:
    """生成"该站点当地时间 N 小时后"的裸字符串 —— 领星就是这个格式。

    偏移量实时从 tzdata 取，不写死"伦敦 = UTC+1"：那样冬令时那半年会假失败。
    """
    from zoneinfo import ZoneInfo
    site_now = dt.datetime.now(ZoneInfo(tz_name))
    return (site_now + dt.timedelta(hours=hours_from_now)).strftime("%Y-%m-%d %H:%M:%S")


class _FakePromoSource:
    name = "fake-promo"
    label = "假促销源"

    def __init__(self, rows, tz_name="Europe/London"):
        self.rows = rows
        self.tz_name = tz_name

    def supports(self, metric):
        return metric == "promotion.active"

    def lag_seconds(self, metric):
        return 0.0

    def fetch(self, metric, scope, window=None):
        from zoneinfo import ZoneInfo

        from ivyea_agent.datasources.lingxing_source import LingxingSource
        tz = ZoneInfo(self.tz_name)
        out = []
        for kind, raw in self.rows:
            row = LingxingSource._promotion(raw, scope.get("sid"), kind, tz)
            row["asins"] = raw.get("_asins", [])
            row["asin_count"] = len(row["asins"])
            out.append(row)
        return out


@pytest.fixture()
def wire_promo(ivyea_home, monkeypatch):
    from ivyea_agent import datasources, metrics

    def _install(rows, tz_name="Europe/London"):
        for s in list(metrics.registered()):
            metrics.unregister(s.name)
        metrics.register(_FakePromoSource(rows, tz_name), priority=1)
        monkeypatch.setattr(datasources, "install_defaults", lambda: None)
    yield _install
    from ivyea_agent import metrics as m
    for s in list(m.registered()):
        m.unregister(s.name)


def _run(sid=1863):
    from ivyea_agent import store_health
    return store_health.check_l1(sid)


def _codes(result):
    return sorted(f.code for f in result.findings)


def _of(result, code):
    return [f for f in result.findings if f.code == code]


# ── 规范化 ──────────────────────────────────────────────────────────────────
def test_site_time_is_parsed_in_store_timezone(ivyea_home):
    """同一个裸时间串，UK 店和 JP 店必须落在不同的绝对时刻（差 8 小时）。"""
    from zoneinfo import ZoneInfo

    from ivyea_agent.datasources.lingxing_source import LingxingSource

    raw = _raw_promo(promotion_end_time="2026-08-24 23:59:00")
    uk = LingxingSource._promotion(raw, 1863, "coupon", ZoneInfo("Europe/London"))
    jp = LingxingSource._promotion(raw, 1872, "coupon", ZoneInfo("Asia/Tokyo"))
    delta = (dt.datetime.fromisoformat(uk["end_at"])
             - dt.datetime.fromisoformat(jp["end_at"])).total_seconds()
    assert delta == 8 * 3600


def test_money_strings_with_currency_symbols_are_parsed(ivyea_home):
    from zoneinfo import ZoneInfo

    from ivyea_agent.datasources.lingxing_source import LingxingSource

    row = LingxingSource._promotion(
        _raw_promo(budget="JP¥10,084.0", cost="8067.20"), 1872, "coupon",
        ZoneInfo("Asia/Tokyo"))
    assert row["budget"] == 10084.0
    assert row["cost"] == 8067.2
    assert row["budget_used_pct"] == 80.0


def test_missing_budget_is_none_not_zero(ivyea_home):
    """"没有预算这个概念"和"预算是 0"在卡片上必须能区分开。"""
    from zoneinfo import ZoneInfo

    from ivyea_agent.datasources.lingxing_source import LingxingSource

    row = LingxingSource._promotion(_raw_promo(budget="", cost=""), 1863, "seckill",
                                    ZoneInfo("Europe/London"))
    assert row["budget"] is None and row["cost"] is None
    assert row["budget_used_pct"] is None


# ── 临期 ────────────────────────────────────────────────────────────────────
def test_ending_within_six_hours_is_crit(wire_promo):
    wire_promo([("coupon", _raw_promo(
        promotion_start_time=_site_time(-40), promotion_end_time=_site_time(5.5),
        last_sync_time=_site_time(-1)))])
    res = _run()
    hit = _of(res, "promo.ending_soon")
    assert hit and hit[0].severity == "crit"
    assert "还有" in hit[0].message and "结束" in hit[0].message


def test_ending_within_24_hours_is_warn(wire_promo):
    wire_promo([("coupon", _raw_promo(
        promotion_start_time=_site_time(-40), promotion_end_time=_site_time(23.5),
        last_sync_time=_site_time(-1)))])
    hit = _of(_run(), "promo.ending_soon")
    assert hit and hit[0].severity == "warn"


def test_only_one_report_per_lead_bucket(wire_promo):
    """落在两档之间（比如还有 12 小时）不报 —— 24 小时那档已经报过了。"""
    wire_promo([("coupon", _raw_promo(
        promotion_start_time=_site_time(-40), promotion_end_time=_site_time(12),
        last_sync_time=_site_time(-1)))])
    assert "promo.ending_soon" not in _codes(_run())


def test_far_future_end_is_silent(wire_promo):
    wire_promo([("coupon", _raw_promo(
        promotion_start_time=_site_time(-40), promotion_end_time=_site_time(24 * 9),
        last_sync_time=_site_time(-1)))])
    assert "promo.ending_soon" not in _codes(_run())


def test_cancelled_promotion_never_counts_down(wire_promo):
    """已取消的活动没有"还剩多久"。"""
    wire_promo([("coupon", _raw_promo(
        origin_status="CANCELED",
        promotion_start_time=_site_time(-40), promotion_end_time=_site_time(5),
        last_sync_time=_site_time(-1)))])
    assert "promo.ending_soon" not in _codes(_run())


def test_expired_promotion_never_counts_down(wire_promo):
    wire_promo([("coupon", _raw_promo(
        promotion_start_time=_site_time(-40), promotion_end_time=_site_time(-2),
        last_sync_time=_site_time(-1)))])
    assert "promo.ending_soon" not in _codes(_run())


def test_starting_soon_is_info_with_a_checklist_hint(wire_promo):
    wire_promo([("seckill", _raw_promo(
        origin_status="APPROVED",
        promotion_start_time=_site_time(23.5), promotion_end_time=_site_time(48),
        last_sync_time=_site_time(-1)))])
    hit = _of(_run(), "promo.starting_soon")
    assert hit and hit[0].severity == "info"
    assert "库存" in hit[0].message


def test_asins_appear_in_the_message(wire_promo):
    """「哪个 ASIN 的券要结束了」—— ASIN 必须出现在提醒里，否则还得自己去查。"""
    raw = _raw_promo(promotion_start_time=_site_time(-40),
                     promotion_end_time=_site_time(5.5), last_sync_time=_site_time(-1))
    raw["_asins"] = ["B0TEST0001", "B0TEST0002"]
    wire_promo([("coupon", raw)])
    hit = _of(_run(), "promo.ending_soon")
    assert hit and "B0TEST0001" in hit[0].message
    assert hit[0].evidence["asins"] == ["B0TEST0001", "B0TEST0002"]


# ── 预算 ────────────────────────────────────────────────────────────────────
def test_coupon_budget_warn_and_crit_bands(wire_promo):
    def run_with(cost):
        wire_promo([("coupon", _raw_promo(
            budget="£1000.00", cost=cost,
            promotion_start_time=_site_time(-10), promotion_end_time=_site_time(200),
            last_sync_time=_site_time(-1)))])
        return _of(_run(), "promo.budget_exhausted")

    assert run_with("700.00") == []                     # 70% 不报
    assert run_with("850.00")[0].severity == "warn"     # 85%
    assert run_with("960.00")[0].severity == "crit"     # 96%


def test_budget_alert_only_for_running_promotions(wire_promo):
    """还没开始的活动预算当然没花 —— 更没有"见底"这回事。"""
    wire_promo([("coupon", _raw_promo(
        origin_status="APPROVED", budget="£1000.00", cost="990.00",
        promotion_start_time=_site_time(60), promotion_end_time=_site_time(200),
        last_sync_time=_site_time(-1)))])
    assert "promo.budget_exhausted" not in _codes(_run())


# ── 数据新鲜度 ──────────────────────────────────────────────────────────────
def test_stale_plugin_sync_is_reported(wire_promo):
    """插件掉线时接口照样返回旧数据 —— 这条报的是"上面的数不能信"。"""
    wire_promo([("coupon", _raw_promo(
        promotion_start_time=_site_time(-100), promotion_end_time=_site_time(300),
        last_sync_time=_site_time(-50)))])
    hit = _of(_run(), "promo.sync_stale")
    assert hit and hit[0].severity == "warn"
    assert "LINGXING助手" in hit[0].message


def test_fresh_sync_is_silent(wire_promo):
    wire_promo([("coupon", _raw_promo(
        promotion_start_time=_site_time(-100), promotion_end_time=_site_time(300),
        last_sync_time=_site_time(-2)))])
    assert "promo.sync_stale" not in _codes(_run())


# ── 动作 ────────────────────────────────────────────────────────────────────
def test_promotion_findings_carry_no_executable_intent(wire_promo):
    """延长活动/加预算在领星和亚马逊官方 API 上都没有写接口（促销只能在后台改）。
    给一个点了没用的按钮，比不给更糟。"""
    wire_promo([("coupon", _raw_promo(
        budget="£1000.00", cost="960.00",
        promotion_start_time=_site_time(-40), promotion_end_time=_site_time(5.5),
        last_sync_time=_site_time(-1)))])
    res = _run()
    promo_findings = [f for f in res.findings if f.code.startswith("promo.")]
    assert promo_findings
    assert all(f.intent is None for f in promo_findings)
    assert all(not f.executable for f in promo_findings)


def test_card_labels_exist_for_every_promo_rule():
    """新规则必须有人话短名 —— 卡片上出现 promo.sync_stale 这种代码是半成品。"""
    from ivyea_agent.feishu_card import RULE_LABEL

    for code in ("promo.ending_soon", "promo.starting_soon",
                 "promo.budget_exhausted", "promo.sync_stale"):
        assert code in RULE_LABEL, code


def test_no_promotions_is_skipped_not_a_gap(wire_promo):
    """本店当前没有活动是正常状态，不是数据缺口 —— 记成缺口会天天告警。"""
    wire_promo([])
    res = _run()
    assert any("促销规则跳过" in s for s in res.skipped)
    assert not [f for f in res.findings if f.code.startswith("promo.")]
