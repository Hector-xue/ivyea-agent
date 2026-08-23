"""§8 降级链与连续失败告警测试。"""
from __future__ import annotations

import pytest


# ── 连续计数 ────────────────────────────────────────────────────────────────
def test_consecutive_not_cumulative(ivyea_home):
    """巡检按小时级反复跑，偶发超时是常态；累计计数迟早触发 = 狼来了。"""
    from ivyea_agent import reliability as r

    assert r.record_failure("k") == 1
    assert r.record_failure("k") == 2
    r.record_success("k")
    assert r.count("k") == 0
    assert r.record_failure("k") == 1        # 重新从 1 开始


def test_alert_fires_only_on_crossing(ivyea_home):
    """只在跨过门槛那一次报，之后持续失败不再轰炸。"""
    from ivyea_agent import reliability as r

    fired = []
    for _ in range(6):
        n = r.record_failure("k")
        if r.should_alert("k", 3):
            fired.append(n)
    assert fired == [3]


def test_keys_are_independent(ivyea_home):
    from ivyea_agent import reliability as r

    r.record_failure("a"); r.record_failure("a"); r.record_failure("b")
    assert r.count("a") == 2 and r.count("b") == 1


def test_corrupt_file_does_not_crash(ivyea_home):
    from ivyea_agent import reliability as r

    r.record_failure("k")
    r._FILE.write_text("{ broken", encoding="utf-8")
    assert r.count("k") == 0                 # 当作没有历史
    assert r.record_failure("k") == 1        # 仍可继续记


def test_detail_is_kept_and_truncated(ivyea_home):
    from ivyea_agent import reliability as r

    r.record_failure("k", "x" * 2000)
    assert 0 < len(r.detail("k")) <= 500


# ── 降级链 ──────────────────────────────────────────────────────────────────
def test_primary_success_no_fallback(ivyea_home, monkeypatch):
    from ivyea_agent import notify

    monkeypatch.setattr(notify, "send_card",
                        lambda *a, **k: {"ok": True, "message_id": "om_1"})
    monkeypatch.setattr(notify, "send", lambda *a, **k: pytest.fail("不该走兜底"))
    r = notify.send_alert("x", card={"a": 1})
    assert r["ok"] and r["degraded"] is False


def test_fallback_to_webhook(ivyea_home, monkeypatch):
    from ivyea_agent import notify

    monkeypatch.setattr(notify, "send_card", lambda *a, **k: {"ok": False, "error": "down"})
    monkeypatch.setattr(notify, "_configured_webhook_url", lambda ch, override="": "https://h/x")
    monkeypatch.setattr(notify, "send", lambda msg, **k: {"ok": True, "channel": "feishu"})
    r = notify.send_alert("正文", card={"a": 1})
    assert r["ok"] and r["degraded"] is True and r["degraded_from"] == "feishu_app"


def test_no_webhook_configured_is_explicit(ivyea_home, monkeypatch):
    """没有兜底通道时要说清楚，不能只报一句"发送失败"。"""
    from ivyea_agent import notify

    monkeypatch.setattr(notify, "send_card", lambda *a, **k: {"ok": False, "error": "down"})
    monkeypatch.setattr(notify, "_configured_webhook_url", lambda ch, override="": "")
    r = notify.send_alert("x", card={"a": 1})
    assert not r["ok"] and "无兜底通道" in r["error"] and "down" in r["error"]


def test_both_channels_fail_lists_all_reasons(ivyea_home, monkeypatch):
    from ivyea_agent import notify

    monkeypatch.setattr(notify, "send_card", lambda *a, **k: {"ok": False, "error": "e1"})
    monkeypatch.setattr(notify, "_configured_webhook_url", lambda ch, override="": "https://h/x")
    monkeypatch.setattr(notify, "send", lambda msg, **k: {"ok": False, "error": "e2"})
    r = notify.send_alert("x", card={"a": 1})
    assert not r["ok"] and "e1" in r["error"] and "e2" in r["error"]
    assert len(r["attempts"]) == 2


# ── 巡检接线 ────────────────────────────────────────────────────────────────
def _gapped(sid, **kw):
    from ivyea_agent import store_health

    res = store_health.CheckResult(sid=sid, layer="L1")
    res.gaps.append("领星取数失败：超时")
    return res


def _clean(sid, **kw):
    from ivyea_agent import store_health
    return store_health.CheckResult(sid=sid, layer="L1")


def test_patrol_alerts_after_three_consecutive_gaps(ivyea_home, monkeypatch):
    from ivyea_agent import notify, reliability, schedule, store_health

    monkeypatch.setattr(store_health, "check_l1", _gapped)
    alerts = []
    monkeypatch.setattr(notify, "send_alert",
                        lambda text, **k: alerts.append(k.get("title")) or {"ok": True})
    for _ in range(5):
        schedule.run_task("store_l1", {"sid": 1, "channel": "feishu_app"})
    assert alerts.count("数据源异常") == 1, "该只在第 3 次报一次"
    assert reliability.count("patrol.store_l1.1") == 5


def test_recovery_resets_the_counter(ivyea_home, monkeypatch):
    from ivyea_agent import notify, reliability, schedule, store_health

    monkeypatch.setattr(notify, "send_alert", lambda text, **k: {"ok": True})
    monkeypatch.setattr(store_health, "check_l1", _gapped)
    schedule.run_task("store_l1", {"sid": 1})
    schedule.run_task("store_l1", {"sid": 1})
    monkeypatch.setattr(store_health, "check_l1", _clean)
    schedule.run_task("store_l1", {"sid": 1})
    assert reliability.count("patrol.store_l1.1") == 0


def test_daily_alerts_after_two(ivyea_home, monkeypatch):
    """早报一天一次，等三次要等三天，所以门槛更低。"""
    from ivyea_agent import notify, schedule, store_health

    monkeypatch.setattr(store_health, "check_l3", _gapped)
    monkeypatch.setattr(store_health, "daily_summary",
                        lambda sid, **kw: {"lines": [], "metrics": {}, "gaps": []})
    alerts = []
    monkeypatch.setattr(notify, "send_alert",
                        lambda text, **k: alerts.append(k.get("title")) or {"ok": True})
    schedule.run_task("store_daily", {"sid": 1})
    assert alerts == []
    schedule.run_task("store_daily", {"sid": 1})
    assert alerts.count("数据源异常") == 1


def test_counters_are_per_store(ivyea_home, monkeypatch):
    from ivyea_agent import notify, reliability, schedule, store_health

    monkeypatch.setattr(store_health, "check_l1", _gapped)
    monkeypatch.setattr(notify, "send_alert", lambda text, **k: {"ok": True})
    schedule.run_task("store_l1", {"sid": 1})
    schedule.run_task("store_l1", {"sid": 2})
    assert reliability.count("patrol.store_l1.1") == 1
    assert reliability.count("patrol.store_l1.2") == 1


# ── §8.4 异步执行预案 ───────────────────────────────────────────────────────
def _appr(ivyea_home):
    from ivyea_agent import approvals, store_health

    return approvals.create(store_health.Finding(
        code="x", layer="L1", severity="warn", action_class=store_health.STANCH,
        sid=1, scope="campaign", target_id="C1", target_name="活动", message="预算 100→85",
        intent={"op_type": "campaign_budget", "sid": 1, "target_id": "C1",
                "change": {"daily_budget": 85.0}, "before": {"daily_budget": 100.0}}),
        chat_id="oc_1")


def test_sync_is_the_default(ivyea_home, monkeypatch):
    """方案明确说异步是预案非默认 —— 不能自作主张改默认行为。"""
    from ivyea_agent import approval_flow, approvals, lingxing_write

    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)
    monkeypatch.setattr(lingxing_write, "execute",
                        lambda i, dry_run=True: {"ok": True, "audit_id": "a", "detail": "d"})
    a = _appr(ivyea_home)
    r = approval_flow.resolve(a.id, "approve", chat_id="oc_1")
    assert r["state"] == approvals.EXECUTED and "async" not in r


def test_async_returns_immediately_and_finishes_in_background(ivyea_home, monkeypatch):
    import time

    from ivyea_agent import approval_flow, approvals, config, lingxing_write

    settings = config.load_settings()
    settings["feishu_async_execute"] = True
    config.save_settings(settings)

    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)

    def _slow(intent, dry_run=True):
        time.sleep(0.3)
        return {"ok": True, "audit_id": "a1", "detail": "已执行"}

    monkeypatch.setattr(lingxing_write, "execute", _slow)
    a = _appr(ivyea_home)
    t0 = time.time()
    r = approval_flow.resolve(a.id, "approve", chat_id="oc_1")
    assert time.time() - t0 < 0.2, "异步模式下不该等写入完成"
    assert r["async"] is True and "执行中" in r["detail"]

    deadline = time.time() + 5
    while time.time() < deadline and approvals.get(a.id).state != approvals.EXECUTED:
        time.sleep(0.05)
    assert approvals.get(a.id).state == approvals.EXECUTED


def test_execution_time_is_recorded(ivyea_home, monkeypatch):
    from ivyea_agent import approval_flow, lingxing_write

    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)
    monkeypatch.setattr(lingxing_write, "execute",
                        lambda i, dry_run=True: {"ok": True, "audit_id": "a", "detail": "d"})
    a = _appr(ivyea_home)
    r = approval_flow.resolve(a.id, "approve", chat_id="oc_1")
    assert "elapsed" in r and r["elapsed"] >= 0
