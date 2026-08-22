"""店铺巡检任务接入 schedule 的测试（P3）。"""
from __future__ import annotations

import pytest


def test_store_tasks_registered(ivyea_home):
    from ivyea_agent import schedule

    for t in ("store_l1", "store_l2", "store_daily", "approvals_expire"):
        assert t in schedule.ALLOWED_TASKS


def test_every_minutes_supported(ivyea_home):
    """L1 每 20 分钟；写成 every_hours=0.333 既不直观又会漂移。"""
    from ivyea_agent import schedule

    job = schedule.set_job("l1", "store_l1", every_minutes=20, args={"sid": 1})
    assert job["every_minutes"] == 20.0
    # 同时回写等价小时数，保持老读取方兼容
    assert job["every_hours"] == pytest.approx(20 / 60)


def test_every_minutes_drives_due(ivyea_home, monkeypatch):
    import time

    from ivyea_agent import schedule

    schedule.set_job("l1", "store_l1", every_minutes=20, args={"sid": 1})
    data = schedule.load()
    data["jobs"][0]["last_run"] = time.time() - 10 * 60      # 10 分钟前
    schedule.save(data)
    assert schedule.due_jobs() == []

    data = schedule.load()
    data["jobs"][0]["last_run"] = time.time() - 21 * 60      # 21 分钟前
    schedule.save(data)
    assert [j["name"] for j in schedule.due_jobs()] == ["l1"]


def test_hours_still_work(ivyea_home):
    from ivyea_agent import schedule

    job = schedule.set_job("daily", "store_daily", every_hours=24, args={"sid": 1})
    assert "every_minutes" not in job and job["every_hours"] == 24.0


def test_task_requires_sid(ivyea_home):
    from ivyea_agent import schedule

    ok, text = schedule.run_task("store_l1", {})
    assert not ok and "缺少 sid" in text


def test_store_task_runs_and_renders(ivyea_home, monkeypatch):
    from ivyea_agent import schedule, store_health

    called = {}

    def _fake(sid):
        called["sid"] = sid
        res = store_health.CheckResult(sid=sid, layer="L1")
        res.findings.append(store_health.Finding(
            code="stock.oos", layer="L1", severity="crit",
            action_class=store_health.ADVISORY, sid=sid, scope="msku",
            target_id="M1", target_name="商品", message="断货了"))
        return res

    monkeypatch.setattr(store_health, "check_l1", _fake)
    ok, text = schedule.run_task("store_l1", {"sid": 1863})
    assert ok and called["sid"] == 1863
    assert "断货了" in text


def test_quiet_when_nothing_found(ivyea_home, monkeypatch):
    """每 20 分钟一次，没异常还推送就是刷屏。"""
    from ivyea_agent import schedule, store_health, notify

    monkeypatch.setattr(store_health, "check_l1",
                        lambda sid: store_health.CheckResult(sid=sid, layer="L1"))
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(k) or {"ok": True})

    ok, _text = schedule.run_task("store_l1", {"sid": 1, "notify": True,
                                               "channel": "stdout"})
    assert ok and sent == []


def test_daily_always_notifies_even_when_clean(ivyea_home, monkeypatch):
    """早报例外：用户要的就是每天确认一眼。"""
    from ivyea_agent import schedule, store_health, notify

    monkeypatch.setattr(store_health, "check_l3",
                        lambda sid, **kw: store_health.CheckResult(sid=sid, layer="L3"))
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(k) or {"ok": True})

    ok, _t = schedule.run_task("store_daily", {"sid": 1, "notify": True,
                                               "channel": "stdout"})
    assert ok and len(sent) == 1


def test_gaps_break_silence(ivyea_home, monkeypatch):
    """没异常但有数据缺口时必须推送——"没告警"不能等于"没问题"。"""
    from ivyea_agent import schedule, store_health, notify

    def _gapped(sid):
        res = store_health.CheckResult(sid=sid, layer="L1")
        res.gaps.append("库存指标无数据源")
        return res

    monkeypatch.setattr(store_health, "check_l1", _gapped)
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(k) or {"ok": True})
    schedule.run_task("store_l1", {"sid": 1, "notify": True, "channel": "stdout"})
    assert len(sent) == 1


def test_approvals_expire_task(ivyea_home):
    from ivyea_agent import approvals, schedule, store_health

    f = store_health.Finding(
        code="x", layer="L2", severity="warn", action_class=store_health.STANCH,
        sid=1, scope="campaign", target_id="C1", target_name="c", message="m",
        intent={"op_type": "campaign_budget", "sid": 1})
    approvals.create(f, ttl_seconds=-1)
    ok, text = schedule.run_task("approvals_expire", {})
    assert ok and "1 条" in text
    assert approvals.summary().get(approvals.EXPIRED) == 1
