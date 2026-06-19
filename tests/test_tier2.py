"""Tier 2：领星缓存、成本护栏（每日花费）、CLI 注册。"""
from __future__ import annotations


def test_cache_hit_and_clear(ivyea_home, monkeypatch):
    from ivyea_agent import lingxing_cache as lc, lingxing_datasets as ds
    calls = {"n": 0}
    monkeypatch.setattr(ds, "fetch_dataset", lambda name, params: calls.__setitem__("n", calls["n"] + 1) or [{"x": 1}])
    r1 = lc.fetch_dataset("sp_keywords", {"sid": 1, "length": 300})
    r2 = lc.fetch_dataset("sp_keywords", {"sid": 1, "length": 300})
    assert r1 == r2 == [{"x": 1}]
    assert calls["n"] == 1          # 第二次命中缓存，不再真取
    assert lc.clear() == 1
    lc.fetch_dataset("sp_keywords", {"sid": 1, "length": 300})
    assert calls["n"] == 2          # 清空后重新真取


def test_cache_force_bypasses(ivyea_home, monkeypatch):
    from ivyea_agent import lingxing_cache as lc, lingxing_datasets as ds
    calls = {"n": 0}
    monkeypatch.setattr(ds, "fetch_dataset", lambda name, params: calls.__setitem__("n", calls["n"] + 1) or [])
    lc.fetch_dataset("sp_keywords", {"sid": 1})
    lc.fetch_dataset("sp_keywords", {"sid": 1}, force=True)
    assert calls["n"] == 2


def test_cache_key_differs_by_params(ivyea_home, monkeypatch):
    from ivyea_agent import lingxing_cache as lc, lingxing_datasets as ds
    calls = {"n": 0}
    monkeypatch.setattr(ds, "fetch_dataset", lambda name, params: calls.__setitem__("n", calls["n"] + 1) or [])
    lc.fetch_dataset("sp_keywords", {"sid": 1})
    lc.fetch_dataset("sp_keywords", {"sid": 2})   # 不同参数 → 不同 key
    assert calls["n"] == 2


def test_daily_spend(ivyea_home):
    from ivyea_agent import pricing
    assert pricing.today_spend() == 0.0
    assert abs(pricing.add_spend(0.5) - 0.5) < 1e-9
    assert abs(pricing.add_spend(0.3) - 0.8) < 1e-9
    assert abs(pricing.today_spend() - 0.8) < 1e-9


def test_daily_limit_setting(ivyea_home):
    from ivyea_agent import pricing, config
    assert pricing.daily_limit() == 0.0
    config.set_setting("daily_cost_limit_cny", 5)
    assert pricing.daily_limit() == 5.0


def test_onboard_registered():
    from ivyea_agent.cli import build_parser
    p = build_parser()
    # onboard 子命令可解析
    args = p.parse_args(["onboard"])
    assert getattr(args, "func", None) is not None
