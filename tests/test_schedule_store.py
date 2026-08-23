"""店铺巡检任务接入 schedule 的测试（P3）。"""
from __future__ import annotations

import pytest


def test_store_tasks_registered(ivyea_home):
    from ivyea_agent import schedule

    for t in ("store_l1", "store_l2", "store_daily", "approvals_expire"):
        assert t in schedule.ALLOWED_TASKS


def test_every_minutes_supported(ivyea_home):
    """分钟级间隔要能直接写分钟；写成 every_hours=0.333 既不直观又会漂移。"""
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
    """巡检按小时级反复跑，没异常还推送就是刷屏。"""
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


# ── 周报 / 月报 ─────────────────────────────────────────────────────────────
# 定位与早报**不同**：早报负责"今天要你拍板的事"（带按钮），
# 周报月报负责回顾（不创建任何审批项）。混在一起会让同一个目标挂两条待办。

def test_period_tasks_registered(ivyea_home):
    from ivyea_agent import schedule

    for t in ("store_weekly", "store_monthly"):
        assert t in schedule.ALLOWED_TASKS
    assert schedule.PERIOD_TASKS["store_weekly"][0] == 7
    assert schedule.PERIOD_TASKS["store_monthly"][0] == 30


def test_patrol_default_cadence_is_single_sourced(ivyea_home):
    """默认间隔只有这一份。前端再写一份的话，生效的永远是小的那个。"""
    from ivyea_agent import feishu_setup, schedule

    assert schedule.PATROL_DEFAULT_MINUTES["store_l1"] == 60.0
    assert schedule.PATROL_DEFAULT_MINUTES["store_l2"] == 720.0
    defaults = feishu_setup.patrol_defaults()
    for key, (_name, task) in feishu_setup.PATROL_JOBS.items():
        assert defaults[key]["every_minutes"] == schedule.PATROL_DEFAULT_MINUTES[task]


def test_enabling_a_report_without_an_interval_uses_its_own_default(ivyea_home):
    """周报没给间隔时必须落到 7 天。回落到"24 小时"就成了每天一份周报。"""
    from ivyea_agent import feishu_setup, schedule

    feishu_setup.configure_patrol({"scope": "all", "weekly": {"enabled": True},
                                   "monthly": {"enabled": True}})
    jobs = {j["name"]: j for j in schedule.load()["jobs"]}
    assert jobs["patrol-weekly"]["every_minutes"] == 7 * 24 * 60
    assert jobs["patrol-monthly"]["every_minutes"] == 30 * 24 * 60


def _stub_period(monkeypatch, findings=()):
    from ivyea_agent import store_health

    class _Res:
        def __init__(self):
            self.findings = list(findings)
            self.gaps = []

        def sorted_findings(self):
            return list(self.findings)

    monkeypatch.setattr(store_health, "check_l3", lambda sid, **k: _Res())
    monkeypatch.setattr(store_health, "period_summary",
                        lambda sid, **k: {"lines": [f"**广告**（本周）　sid {sid}"],
                                          "metrics": {}, "gaps": []})


def test_weekly_report_never_creates_approvals(ivyea_home, monkeypatch):
    """同一条建议早报已经给过按钮；周报再创建一遍，同一个目标就挂两条待办，
    批了其中一条另一条还在，很容易对同一个活动改两次预算。"""
    from ivyea_agent import approvals, patrol_push, schedule, stores

    _stub_period(monkeypatch)
    monkeypatch.setattr(stores, "resolve_targets",
                        lambda args: [{"sid": 1863, "name": "欧洲-UK", "has_ads": True}])
    sent = {}
    monkeypatch.setattr(patrol_push, "push_period",
                        lambda rows, **kw: sent.update(kw, rows=rows) or
                        {"ok": True, "message_id": "om_1"})
    before = len(approvals.list_items(limit=999))
    ok, text = schedule.run_task("store_weekly", {"sids": "all", "notify": True,
                                                  "channel": "feishu_app"})
    assert ok and "周报" in text
    assert sent["period"] == "周报" and "activity" in sent
    assert len(approvals.list_items(limit=999)) == before


def test_period_report_falls_back_to_text_without_feishu(ivyea_home, monkeypatch):
    """channel=stdout 时不发卡片，但汇总正文照出——CLI 下也要能看。"""
    from ivyea_agent import schedule, stores

    _stub_period(monkeypatch)
    monkeypatch.setattr(stores, "resolve_targets",
                        lambda args: [{"sid": 1, "name": "店A", "has_ads": True},
                                      {"sid": 2, "name": "店B", "has_ads": True}])
    ok, text = schedule.run_task("store_monthly", {"sids": "all"})
    assert ok and "月报" in text and "店A" in text and "店B" in text


def test_period_report_isolates_a_broken_store(ivyea_home, monkeypatch):
    """一个店取数炸了，其余店照常出报 —— 与巡检的逐店隔离同一条纪律。"""
    from ivyea_agent import schedule, store_health, stores

    _stub_period(monkeypatch)
    real = store_health.check_l3

    def _maybe_boom(sid, **k):
        if str(sid) == "2":
            raise RuntimeError("领星超时")
        return real(sid, **k)

    monkeypatch.setattr(store_health, "check_l3", _maybe_boom)
    monkeypatch.setattr(stores, "resolve_targets",
                        lambda args: [{"sid": 1, "name": "店A"}, {"sid": 2, "name": "店B"}])
    ok, text = schedule.run_task("store_weekly", {"sids": "all"})
    assert ok is False                       # 有店失败就不算全绿
    assert "店A" in text and "取数失败" in text
