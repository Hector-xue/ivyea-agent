"""多店铺巡检：店铺清单、能力门控、逐店隔离、汇总早报。

这批用例守的是几个只在多店场景才暴露的坑（都是实测领星数据发现的）：
- TR/PL 未开通广告，调广告接口稳定 code=102 —— 不能当故障计入连续失败告警。
- 同一批货铺 11 个欧洲站，UK/DE 之间 112 个 MSKU 同名 —— 审批键必须带 sid。
- 店铺清单本身挂掉时，巡检要退回陈旧缓存继续跑，而不是 11 个店一起哑掉。
"""
from __future__ import annotations

import json

import pytest

SELLERS = [
    {"sid": 1863, "name": "欧洲-UK", "region": "EU", "country": "英国",
     "marketplace_id": "A1F83G8C2ARO7P", "seller_id": "S1", "status": 2,
     "has_ads_setting": 1},
    {"sid": 1870, "name": "欧洲-TR", "region": "TR", "country": "土耳其",
     "marketplace_id": "A33AVAJ2PDY3EV", "seller_id": "S1", "status": 2,
     "has_ads_setting": 0},
    {"sid": 1899, "name": "已停用店", "region": "EU", "country": "德国",
     "marketplace_id": "A1PA6795UKMFR9", "seller_id": "S1", "status": 0,
     "has_ads_setting": 1},
]


@pytest.fixture()
def fake_sellers(monkeypatch, ivyea_home):
    """把领星店铺列表换成固定样本，并记录调用次数（用于验证缓存）。"""
    calls = {"n": 0}

    def _list():
        calls["n"] += 1
        return [dict(r) for r in SELLERS]

    from ivyea_agent import lingxing_datasets
    monkeypatch.setattr(lingxing_datasets, "list_sellers", _list)
    return calls


# ── 清单与缓存 ──────────────────────────────────────────────────────────────
def test_list_stores_normalizes_and_filters_inactive(fake_sellers):
    from ivyea_agent import stores
    rows = stores.list_stores()
    assert [r["sid"] for r in rows] == [1863, 1870]          # 停用的被滤掉
    assert rows[0]["name"] == "欧洲-UK"
    assert rows[0]["has_ads"] is True
    assert rows[1]["has_ads"] is False


def test_list_stores_includes_inactive_when_asked(fake_sellers):
    from ivyea_agent import stores
    assert len(stores.list_stores(include_inactive=True)) == 3


def test_list_stores_uses_cache(fake_sellers):
    from ivyea_agent import stores
    stores.list_stores()
    stores.list_stores()
    stores.list_stores()
    assert fake_sellers["n"] == 1, "清单应命中缓存，不该每次都联网"


def test_list_stores_force_refetches(fake_sellers):
    from ivyea_agent import stores
    stores.list_stores()
    stores.list_stores(force=True)
    assert fake_sellers["n"] == 2


def test_list_stores_refetches_after_ttl(fake_sellers):
    from ivyea_agent import stores
    stores.list_stores()
    stores.list_stores(ttl=0.0)
    assert fake_sellers["n"] == 2


def test_stale_cache_survives_fetch_failure(fake_sellers, monkeypatch):
    """清单接口挂了要退回陈旧缓存 —— 元数据故障不该放大成全店失明。"""
    from ivyea_agent import lingxing_datasets, stores
    stores.list_stores()                                      # 先建立缓存

    def _boom():
        raise RuntimeError("领星挂了")

    monkeypatch.setattr(lingxing_datasets, "list_sellers", _boom)
    rows = stores.list_stores(ttl=0.0)                        # 强制过期 → 触发拉取
    assert [r["sid"] for r in rows] == [1863, 1870]


def test_no_cache_and_fetch_failure_raises(monkeypatch, ivyea_home):
    """连缓存都没有时必须抛错：那种情况确实无从知道要巡检谁，不能装作正常。"""
    from ivyea_agent import lingxing_datasets, stores

    def _boom():
        raise RuntimeError("领星挂了")

    monkeypatch.setattr(lingxing_datasets, "list_sellers", _boom)
    with pytest.raises(RuntimeError):
        stores.list_stores()


def test_corrupt_cache_is_ignored(fake_sellers):
    from ivyea_agent import stores
    stores.STORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    stores.STORES_FILE.write_text("{ 这不是 json", encoding="utf-8")
    assert len(stores.list_stores()) == 2                     # 坏缓存当作没有，重新拉


# ── 目标解析 ────────────────────────────────────────────────────────────────
def test_resolve_single_sid_backward_compatible(fake_sellers):
    from ivyea_agent import stores
    targets = stores.resolve_targets({"sid": 1863})
    assert len(targets) == 1 and targets[0]["name"] == "欧洲-UK"


def test_resolve_all(fake_sellers):
    from ivyea_agent import stores
    assert [t["sid"] for t in stores.resolve_targets({"sids": "all"})] == [1863, 1870]


def test_resolve_explicit_list(fake_sellers):
    from ivyea_agent import stores
    targets = stores.resolve_targets({"sids": [1870, 1863]})
    assert [str(t["sid"]) for t in targets] == ["1870", "1863"]  # 保持传入顺序


def test_resolve_exclude(fake_sellers):
    from ivyea_agent import stores
    targets = stores.resolve_targets({"sids": "all", "exclude_sids": [1870]})
    assert [t["sid"] for t in targets] == [1863]


def test_resolve_empty_when_nothing_specified(fake_sellers):
    from ivyea_agent import stores
    assert stores.resolve_targets({}) == []


def test_resolve_unknown_sid_still_runs(fake_sellers):
    """清单里没有的 sid 也要能跑：宁可跑一次报缺口，也不要静默不巡检。"""
    from ivyea_agent import stores
    targets = stores.resolve_targets({"sids": [9999]})
    assert len(targets) == 1
    assert targets[0]["name"] == "sid 9999"
    assert targets[0]["has_ads"] is True


def test_supports_ads_defaults_true_without_catalog(monkeypatch, ivyea_home):
    """清单不可用时按「支持」处理 —— 静默跳过会让人误以为没告警＝没问题。"""
    from ivyea_agent import lingxing_datasets, stores
    monkeypatch.setattr(lingxing_datasets, "list_sellers",
                        lambda: (_ for _ in ()).throw(RuntimeError("挂了")))
    assert stores.supports_ads(1863) is True


def test_supports_ads_reads_flag(fake_sellers):
    from ivyea_agent import stores
    assert stores.supports_ads(1863) is True
    assert stores.supports_ads(1870) is False


def test_name_of_falls_back(fake_sellers):
    from ivyea_agent import stores
    assert stores.name_of(1863) == "欧洲-UK"
    assert stores.name_of(4242) == "sid 4242"


# ── 能力门控：未开通广告的店不该产生数据缺口 ────────────────────────────────
def test_l1_skips_ads_for_store_without_ads(fake_sellers, monkeypatch):
    from ivyea_agent import datasources, metrics, store_health

    monkeypatch.setattr(datasources, "install_defaults", lambda: None)
    seen: list[str] = []

    def _get(metric, scope=None, window=None):
        seen.append(metric)
        return metrics.MetricResult(metric, [])          # 空但成功，不产生缺口

    monkeypatch.setattr(store_health.metrics, "get_metric", _get)
    res = store_health.check_l1(1870)

    assert metrics.ADS_CAMPAIGN_CONFIG.key not in seen, "未开通广告的店不该发这次请求"
    assert any(store_health.ADS_NOT_ENABLED in s for s in res.skipped)
    assert not res.gaps


def test_l1_still_fetches_ads_for_enabled_store(fake_sellers, monkeypatch):
    from ivyea_agent import datasources, metrics, store_health

    monkeypatch.setattr(datasources, "install_defaults", lambda: None)
    seen: list[str] = []

    def _get(metric, scope=None, window=None):
        seen.append(metric)
        return metrics.MetricResult(metric, [])

    monkeypatch.setattr(store_health.metrics, "get_metric", _get)
    store_health.check_l1(1863)
    assert metrics.ADS_CAMPAIGN_CONFIG.key in seen


def test_l2_short_circuits_without_ads(fake_sellers):
    from ivyea_agent import store_health
    res = store_health.check_l2(1870)
    assert res.findings == [] and res.gaps == []
    assert any(store_health.ADS_NOT_ENABLED in s for s in res.skipped)


def test_gap_metrics_records_metric_key(monkeypatch, ivyea_home):
    """数据缺口要留下**指标 key**，不能只留一句中文让上层用正则去抠。"""
    from ivyea_agent import metrics, store_health
    res = store_health.CheckResult(sid=1, layer="L1")
    res.add_gap(metrics.MetricResult(
        "ads.campaign_config", [],
        gap=metrics.DataGap("ads.campaign_config", "参数不合法")))
    assert res.gap_metrics == {"ads.campaign_config": "参数不合法"}
    assert res.gaps and "ads.campaign_config" in res.gaps[0]


# ── 逐店隔离 ────────────────────────────────────────────────────────────────
def test_one_store_failure_does_not_kill_the_batch(fake_sellers, monkeypatch):
    from ivyea_agent import schedule, store_health

    def _check(sid):
        if str(sid) == "1863":
            raise RuntimeError("这个店炸了")
        return store_health.CheckResult(sid=sid, layer="L1")

    monkeypatch.setattr(store_health, "check_l1", _check)
    ok, text = schedule.run_task("store_l1", {"sids": "all"})
    assert ok is False                                   # 整体判失败
    assert "这个店炸了" in text                            # 坏店如实报出
    assert "欧洲-TR" in text                              # 好店照常跑完


def test_multi_store_output_names_each_store(fake_sellers, monkeypatch):
    from ivyea_agent import schedule, store_health
    monkeypatch.setattr(store_health, "check_l1",
                        lambda sid: store_health.CheckResult(sid=sid, layer="L1"))
    ok, text = schedule.run_task("store_l1", {"sids": "all"})
    assert ok is True
    assert "欧洲-UK（sid 1863）" in text and "欧洲-TR（sid 1870）" in text
    assert "成功 2，失败 0" in text


def test_single_store_output_unchanged(fake_sellers, monkeypatch):
    """单店输出必须与旧版逐字一致 —— IvyeaOps 与既有 job 都在解析它。"""
    from ivyea_agent import schedule, store_health
    monkeypatch.setattr(store_health, "check_l1",
                        lambda sid: store_health.CheckResult(sid=sid, layer="L1"))
    ok, text = schedule.run_task("store_l1", {"sid": 1863})
    assert ok is True
    assert text == store_health.render(store_health.CheckResult(sid=1863, layer="L1"))
    assert "——" not in text                               # 不该出现多店的分节头


def test_missing_target_is_an_error(fake_sellers):
    from ivyea_agent import schedule
    ok, text = schedule.run_task("store_l1", {})
    assert ok is False and "sids" in text


def test_reliability_counter_is_per_store(fake_sellers, monkeypatch):
    """一个店的连续失败不该污染另一个店的健康度计数。"""
    from ivyea_agent import reliability, schedule, store_health

    def _check(sid):
        res = store_health.CheckResult(sid=sid, layer="L1")
        if str(sid) == "1863":
            res.gaps.append("指标 x 无数据：坏了")
        return res

    monkeypatch.setattr(store_health, "check_l1", _check)
    monkeypatch.setattr("ivyea_agent.notify.send_alert", lambda *a, **k: {"ok": True})
    for _ in range(3):
        schedule.run_task("store_l1", {"sids": "all"})
    assert reliability.count("patrol.store_l1.1863") == 3
    assert reliability.count("patrol.store_l1.1870") == 0


# ── 汇总早报 ────────────────────────────────────────────────────────────────
def _finding(sid, target_id, code, severity="warn", intent=None):
    from ivyea_agent.store_health import Finding
    return Finding(code=code, layer="L3", severity=severity, action_class="stanch",
                   sid=sid, scope="msku", target_id=target_id, target_name=target_id,
                   message=f"{target_id} 出事了", intent=intent)


def test_multi_store_approval_key_includes_sid():
    """UK 与 DE 有 112 个同名 MSKU；键不带 sid 就会把按钮绑到别的国家去。"""
    from ivyea_agent import feishu_card
    a = _finding(1863, "L4-NDXL-BULA", "listing.rating_low")
    b = _finding(1865, "L4-NDXL-BULA", "listing.rating_low")
    assert feishu_card.multi_store_key(a) != feishu_card.multi_store_key(b)


def test_multi_store_card_lists_every_store():
    from ivyea_agent import feishu_card
    card = feishu_card.build_multi_store_daily_card(date="2026-08-23", stores=[
        {"name": "欧洲-UK", "sid": 1863, "metrics_lines": ["**广告**　花费 1.00"],
         "findings": [_finding(1863, "A", "x", "crit")], "gaps": []},
        {"name": "日本-JP", "sid": 1872, "metrics_lines": [],
         "findings": [], "gaps": ["指标 y 无数据"]},
    ])
    blob = json.dumps(card, ensure_ascii=False)
    assert "欧洲-UK" in blob and "日本-JP" in blob
    assert "2 个店铺" in blob
    assert card["header"]["template"] == "red"           # 最坏一条决定标题颜色


def test_multi_store_card_is_blue_when_all_clean():
    from ivyea_agent import feishu_card
    card = feishu_card.build_multi_store_daily_card(date="2026-08-23", stores=[
        {"name": "欧洲-UK", "sid": 1863, "metrics_lines": [], "findings": [], "gaps": []},
        {"name": "日本-JP", "sid": 1872, "metrics_lines": [], "findings": [], "gaps": []},
    ])
    assert card["header"]["template"] == "blue"
    assert "✅" in json.dumps(card, ensure_ascii=False)


def test_multi_store_card_button_binds_right_approval():
    from ivyea_agent import feishu_card
    uk = _finding(1863, "SAME-MSKU", "ads.acos_breach", intent={"op_type": "campaign_budget"})
    de = _finding(1865, "SAME-MSKU", "ads.acos_breach", intent={"op_type": "campaign_budget"})
    ids = {feishu_card.multi_store_key(uk): "appr-uk",
           feishu_card.multi_store_key(de): "appr-de"}
    card = feishu_card.build_multi_store_daily_card(date="2026-08-23", stores=[
        {"name": "欧洲-UK", "sid": 1863, "metrics_lines": [], "findings": [uk], "gaps": []},
        {"name": "欧洲-DE", "sid": 1865, "metrics_lines": [], "findings": [de], "gaps": []},
    ], approval_ids=ids)
    blob = json.dumps(card, ensure_ascii=False)
    assert "appr-uk" in blob and "appr-de" in blob       # 两条各自绑对，没有互相覆盖


def test_multi_store_card_caps_long_lists():
    from ivyea_agent import feishu_card
    many = [{"name": f"店{i}", "sid": i, "metrics_lines": [],
             "findings": [_finding(i, f"T{i}", "x")], "gaps": []} for i in range(20)]
    blob = json.dumps(feishu_card.build_multi_store_daily_card(
        date="2026-08-23", stores=many), ensure_ascii=False)
    assert "另有" in blob                                 # 超出上限的折叠掉，不撑爆卡片


def test_daily_multi_pushes_one_card(fake_sellers, monkeypatch):
    from ivyea_agent import patrol_push, schedule, store_health

    def _l3(sid, days=7, include_optimizer=True):
        return store_health.CheckResult(sid=sid, layer="L3")

    monkeypatch.setattr(store_health, "check_l3", _l3)
    monkeypatch.setattr(store_health, "daily_summary",
                        lambda sid, days=1: {"lines": ["**广告**　x"], "metrics": {}, "gaps": []})
    sent = {"n": 0}

    def _push(rows, **kw):
        sent["n"] += 1
        assert len(rows) == 2                            # 两个店合成一张卡
        return {"ok": True, "message_id": "om_1", "approvals": [], "stores": len(rows)}

    monkeypatch.setattr(patrol_push, "push_daily_multi", _push)
    ok, text = schedule.run_task("store_daily", {"sids": "all", "channel": "feishu_app"})
    assert ok is True and sent["n"] == 1
    assert "2 个店" in text


def test_daily_multi_survives_one_bad_store(fake_sellers, monkeypatch):
    from ivyea_agent import patrol_push, schedule, store_health

    def _l3(sid, days=7, include_optimizer=True):
        if str(sid) == "1863":
            raise RuntimeError("取数炸了")
        return store_health.CheckResult(sid=sid, layer="L3")

    monkeypatch.setattr(store_health, "check_l3", _l3)
    monkeypatch.setattr(store_health, "daily_summary",
                        lambda sid, days=1: {"lines": [], "metrics": {}, "gaps": []})
    monkeypatch.setattr(patrol_push, "push_daily_multi",
                        lambda rows, **kw: {"ok": True, "message_id": "om_1",
                                            "approvals": [], "stores": len(rows)})
    ok, text = schedule.run_task("store_daily", {"sids": "all", "channel": "feishu_app"})
    assert ok is False                                   # 有店失败 → 整体判失败
    assert "取数炸了" in text
    assert "欧洲-TR" in text                              # 好店的早报照发


def test_daily_multi_all_failed_reports_error(fake_sellers, monkeypatch):
    from ivyea_agent import schedule, store_health

    def _boom(sid, days=7, include_optimizer=True):
        raise RuntimeError("全挂了")

    monkeypatch.setattr(store_health, "check_l3", _boom)
    ok, text = schedule.run_task("store_daily", {"sids": "all", "channel": "feishu_app"})
    assert ok is False and "所有店铺" in text


# ── 领星 listing 字段映射 ───────────────────────────────────────────────────
def test_volume_7_derived_from_average():
    """领星 erp_listing 没有 seven_volume 字段，7 日销量必须由日均反推。"""
    from ivyea_agent.datasources.lingxing_mcp_source import LingxingMcpSource
    row = LingxingMcpSource._listing({"msku": "M1", "average_seven_volume": "3.5"}, 1863)
    assert row["volume_7"] == pytest.approx(24.5)
    assert row["avg_volume_7"] == pytest.approx(3.5)


def test_volume_7_prefers_real_field_if_it_ever_appears():
    """若领星哪天补上了这个字段，优先用真值而不是反推值。"""
    from ivyea_agent.datasources.lingxing_mcp_source import LingxingMcpSource
    row = LingxingMcpSource._listing(
        {"msku": "M1", "seven_volume": "30", "average_seven_volume": "3.5"}, 1863)
    assert row["volume_7"] == pytest.approx(30.0)


# ── 变体合并 ────────────────────────────────────────────────────────────────
def _listing_finding(sid, msku, parent, code="listing.rating_low",
                     severity="warn", intent=None):
    from ivyea_agent.store_health import Finding
    return Finding(code=code, layer="L1", severity=severity, action_class="advisory",
                   sid=sid, scope="msku", target_id=msku, target_name=msku,
                   message=f"「{msku}」评分低", intent=intent, group_id=parent)


def test_collapse_merges_same_parent(ivyea_home):
    """实测日本站一个母体挂 30 个变体，全是 3.0 星——不合并就把早报刷满。"""
    from ivyea_agent import store_health
    fs = [_listing_finding(1872, f"M{i}", "B09TKZ4K8H") for i in range(24)]
    out = store_health.collapse_variants(fs)
    assert len(out) == 1
    assert out[0].group_size == 24
    assert "另有 23 个变体" in out[0].message
    assert out[0].evidence["variant_count"] == 24
    assert out[0].evidence["parent_asin"] == "B09TKZ4K8H"


def test_collapse_keeps_small_groups_intact(ivyea_home):
    """两三条不合并——合并是治刷屏，不是用来藏信息。"""
    from ivyea_agent import store_health
    fs = [_listing_finding(1872, f"M{i}", "P1") for i in range(2)]
    assert len(store_health.collapse_variants(fs)) == 2


def test_collapse_never_merges_executable_findings(ivyea_home):
    """带 intent 的是会真去改钱的动作，合并等于说不清改了哪个目标。"""
    from ivyea_agent import store_health
    fs = [_listing_finding(1872, f"M{i}", "P1", intent={"op_type": "campaign_budget"})
          for i in range(5)]
    out = store_health.collapse_variants(fs)
    assert len(out) == 5
    assert all(f.intent is not None for f in out)


def test_collapse_groups_are_per_code(ivyea_home):
    """同一个母体下不同规则各归各的，不能混成一条。"""
    from ivyea_agent import store_health
    fs = ([_listing_finding(1872, f"A{i}", "P1", code="listing.rating_low") for i in range(4)]
          + [_listing_finding(1872, f"B{i}", "P1", code="rank.drop") for i in range(4)])
    out = store_health.collapse_variants(fs)
    assert len(out) == 2
    assert {f.code for f in out} == {"listing.rating_low", "rank.drop"}


def test_collapse_takes_worst_severity(ivyea_home):
    """合并后的严重度取组内最坏的一条，不能被多数的 warn 稀释掉 crit。"""
    from ivyea_agent import store_health
    fs = [_listing_finding(1872, f"M{i}", "P1") for i in range(4)]
    fs[2].severity = "crit"
    out = store_health.collapse_variants(fs)
    assert len(out) == 1 and out[0].severity == "crit"


def test_collapse_leaves_ungrouped_findings_alone(ivyea_home):
    """没有分组键的（库存、广告类规则）原样通过。"""
    from ivyea_agent import store_health
    fs = [_listing_finding(1872, f"M{i}", "") for i in range(5)]
    assert len(store_health.collapse_variants(fs)) == 5


def test_collapse_threshold_is_configurable(ivyea_home):
    from ivyea_agent import store_health
    store_health.set_threshold("variants.collapse.min", 10)
    fs = [_listing_finding(1872, f"M{i}", "P1") for i in range(5)]
    assert len(store_health.collapse_variants(fs)) == 5   # 5 < 10，不合并
