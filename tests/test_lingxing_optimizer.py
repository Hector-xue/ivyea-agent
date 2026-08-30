"""lingxing_optimizer：用 fixture 数据喂规则引擎，验各杠杆逻辑（不打活接口）。"""
from __future__ import annotations

import pytest


@pytest.fixture()
def patched(ivyea_home, monkeypatch):
    """monkeypatch fetch_dataset + 毛利，喂确定性 fixture。"""
    from ivyea_agent import lingxing_optimizer as opt

    # 单日报表数据（窗口逐日都返回同一份；窗口聚合会累加 → 放大，故只放 1 天有数据）
    search_rows_by_date = {}

    def fake_fetch(name, params):
        date = params.get("report_date")
        if name == "sp_search_term_report":
            return search_rows_by_date.get(date, [])
        if name == "sp_keyword_report":
            return []
        if name == "sp_campaign_report":
            return []
        if name == "asin_profit":
            return [{"grossRate": "40"}]  # 毛利 40% → 目标 ACOS 28%
        return []

    monkeypatch.setattr(opt, "fetch_dataset", fake_fetch)
    return opt, search_rows_by_date


def _one_day(opt, rows_holder, rows):
    # 只在窗口的第一个有效日放数据，避免逐日累加放大
    from ivyea_agent.lingxing_optimizer import _window_dates
    day = _window_dates(30, 2)[0]
    rows_holder[day] = rows
    return day


def test_negative_lever(patched):
    opt, holder = patched
    _one_day(opt, holder, [
        {"campaign_id": "C1", "query": "junk term", "clicks": 20, "orders": 0, "cost": 15, "sales": 0},
    ])
    res = opt.run_store(1876, days=30)
    negs = [c for c in res["candidates"] if c["lever"] == "否词"]
    assert len(negs) == 1
    assert negs[0]["target_name"] == "junk term"
    assert negs[0]["blocked"] is False
    # 目标 ACOS = 0.7 × 40% = 28%
    assert abs(res["target_acos"] - 0.28) < 1e-6


def test_harvest_lever(patched):
    opt, holder = patched
    _one_day(opt, holder, [
        {"campaign_id": "C1", "query": "winner term", "clicks": 30, "orders": 5, "cost": 10, "sales": 100},
    ])
    res = opt.run_store(1876, days=30)
    harv = [c for c in res["candidates"] if c["lever"] == "收割"]
    assert len(harv) == 1 and harv[0]["target_name"] == "winner term"
    assert harv[0]["suggested_bid"] > 0


def test_below_threshold_no_candidate(patched):
    opt, holder = patched
    _one_day(opt, holder, [
        {"campaign_id": "C1", "query": "meh", "clicks": 5, "orders": 0, "cost": 2, "sales": 0},
    ])
    res = opt.run_store(1876, days=30)
    assert res["count"] == 0  # 5 点击 < 15，不否


def test_rejected_term_blocked(patched):
    from ivyea_agent import memory
    opt, holder = patched
    memory.record_decision("sid:1876", "junk term", "negative", "reject")
    _one_day(opt, holder, [
        {"campaign_id": "C1", "query": "junk term", "clicks": 20, "orders": 0, "cost": 15, "sales": 0},
    ])
    res = opt.run_store(1876, days=30)
    negs = [c for c in res["candidates"] if c["lever"] == "否词"]
    assert len(negs) == 1 and negs[0]["blocked"] is True
    assert "否决" in negs[0]["block_reason"]


# ---- 否词护栏接线（term_taxonomy 有没有真的挂到引擎上）----

def test_brand_term_is_blocked_in_engine(patched, monkeypatch):
    """配了品牌词后，品牌搜索词必须在引擎里被拦下——否掉它会直接掐掉品牌流量。"""
    opt, holder = patched
    monkeypatch.setattr(opt, "_cfg", lambda k: {"lingxing_brand_tokens": "ivyea"}.get(
        k, opt._DEFAULTS.get(k)))
    _one_day(opt, holder, [
        {"campaign_id": "C1", "query": "ivyea karaoke", "clicks": 20, "orders": 0, "cost": 15, "sales": 0},
        {"campaign_id": "C1", "query": "free music download", "clicks": 20, "orders": 0, "cost": 15, "sales": 0},
    ])
    res = opt.run_store(1876, days=30)
    negs = {c["target_name"]: c for c in res["candidates"] if c["lever"] == "否词"}
    assert negs["ivyea karaoke"]["blocked"] is True
    assert "品牌词" in negs["ivyea karaoke"]["block_reason"]
    assert negs["ivyea karaoke"]["term_category"] == "brand_term"
    # 普通无效词照样放行，护栏不能把杠杆整体废掉
    assert negs["free music download"]["blocked"] is False


def test_missing_brand_config_surfaces_warning(patched):
    """没配品牌词时不静默放行，候选上要带得出警告。"""
    opt, holder = patched
    _one_day(opt, holder, [
        {"campaign_id": "C1", "query": "junk term", "clicks": 20, "orders": 0, "cost": 15, "sales": 0},
    ])
    res = opt.run_store(1876, days=30)
    neg = [c for c in res["candidates"] if c["lever"] == "否词"][0]
    assert neg["blocked"] is False
    assert any("未配置品牌词" in w for w in neg["guard_warnings"])


def test_uncovered_guards_reported(patched):
    opt, holder = patched
    _one_day(opt, holder, [
        {"campaign_id": "C1", "query": "junk term", "clicks": 20, "orders": 0, "cost": 15, "sales": 0},
    ])
    res = opt.run_store(1876, days=30)
    assert any("新品期" in item for item in res["uncovered_guards"])

    from ivyea_agent import lingxing_report
    md = lingxing_report.render_md(res)
    assert "护栏未覆盖项" in md
    assert "⚠️" in md          # 警告要出现在报告里
