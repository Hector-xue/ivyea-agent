"""doctor 对巡检/飞书子系统的体检，以及 approval cancel。"""
from __future__ import annotations



def _finding(**over):
    from ivyea_agent import store_health

    kw = dict(code="x", layer="L1", severity="warn", action_class=store_health.STANCH,
              sid=1, scope="campaign", target_id="C1", target_name="活动",
              message="预算 100→85",
              intent={"op_type": "campaign_budget", "sid": 1, "target_id": "C1",
                      "change": {"daily_budget": 85.0}, "before": {"daily_budget": 100.0}})
    kw.update(over)
    return store_health.Finding(**kw)


def _named(checks, name):
    return next(c for c in checks if c.name == name)


# ── 撤销 ────────────────────────────────────────────────────────────────────
def test_cancel_pending_and_approved(ivyea_home, monkeypatch):
    """批准后、写开关补开前改主意是真实场景。没有撤销的话那条 intent
    会一直挂着，等哪天开了开关被 execute 捞起来执行。"""
    from ivyea_agent import approval_flow, approvals, lingxing_write

    a = approvals.create(_finding())
    assert approvals.cancel(a.id, "不要了") is True
    assert approvals.get(a.id).state == approvals.DENIED

    b = approvals.create(_finding())
    monkeypatch.setattr(approval_flow, "_update_card", lambda x, c: True)
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: False)
    approval_flow.resolve(b.id, "approve", chat_id="")
    assert approvals.get(b.id).state == approvals.APPROVED
    assert approvals.cancel(b.id) is True
    assert approvals.get(b.id).state == approvals.DENIED


def test_cancel_refuses_executed(ivyea_home, monkeypatch):
    """已执行的只能回滚，不能一撤了之 —— 钱已经动了。"""
    from ivyea_agent import approval_flow, approvals, lingxing_write

    monkeypatch.setattr(approval_flow, "_update_card", lambda x, c: True)
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)
    monkeypatch.setattr(lingxing_write, "execute",
                        lambda i, dry_run=True: {"ok": True, "audit_id": "a", "detail": "d"})
    a = approvals.create(_finding())
    approval_flow.resolve(a.id, "approve", chat_id="")
    assert approvals.cancel(a.id) is False
    assert approvals.get(a.id).state == approvals.EXECUTED


def test_cancelled_item_is_not_executable(ivyea_home, monkeypatch):
    from ivyea_agent import approval_flow, approvals, lingxing_write

    monkeypatch.setattr(approval_flow, "_update_card", lambda x, c: True)
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: False)
    a = approvals.create(_finding())
    approval_flow.resolve(a.id, "approve", chat_id="")
    approvals.cancel(a.id)
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)
    r = approval_flow.execute_approved(a.id)
    assert not r["ok"] and r["reason"] == "not_approved"


# ── doctor ──────────────────────────────────────────────────────────────────
def test_doctor_flags_missing_feishu(ivyea_home, monkeypatch):
    from ivyea_agent import doctor, feishu_client

    monkeypatch.setattr(feishu_client, "is_configured", lambda: False)
    assert _named(doctor.run_checks(), "飞书").status == "warn"


def test_doctor_flags_missing_default_chat(ivyea_home, monkeypatch):
    from ivyea_agent import doctor, feishu_client

    monkeypatch.setattr(feishu_client, "is_configured", lambda: True)
    monkeypatch.setattr(feishu_client, "default_chat_id", lambda: "")
    c = _named(doctor.run_checks(), "飞书")
    assert c.status == "warn" and "发给谁" in c.fix


def test_doctor_flags_no_patrol_jobs(ivyea_home):
    from ivyea_agent import doctor

    assert _named(doctor.run_checks(), "店铺巡检").status == "warn"


def test_doctor_flags_registered_but_never_run(ivyea_home):
    """注册了却从没跑过 —— 多半是 timer 没装。这种"以为在跑其实没跑"最危险。"""
    from ivyea_agent import doctor, schedule

    schedule.set_job("l1", "store_l1", every_minutes=20, args={"sid": 1})
    c = _named(doctor.run_checks(), "店铺巡检")
    assert c.status == "warn" and "从未执行" in c.detail
    assert "list-timers" in c.fix


def test_doctor_ok_when_jobs_have_run(ivyea_home):
    import time

    from ivyea_agent import doctor, schedule

    schedule.set_job("l1", "store_l1", every_minutes=20, args={"sid": 1})
    data = schedule.load()
    data["jobs"][0]["last_run"] = time.time()
    schedule.save(data)
    assert _named(doctor.run_checks(), "店铺巡检").status == "ok"


def test_doctor_flags_stuck_approvals(ivyea_home, monkeypatch):
    from ivyea_agent import approval_flow, approvals, doctor, lingxing_write

    monkeypatch.setattr(approval_flow, "_update_card", lambda x, c: True)
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: False)
    a = approvals.create(_finding())
    approval_flow.resolve(a.id, "approve", chat_id="")
    c = _named(doctor.run_checks(), "待审批动作")
    assert c.status == "warn" and "写开关" in c.fix


def test_doctor_flags_failed_approvals(ivyea_home, monkeypatch):
    from ivyea_agent import approvals, doctor

    a = approvals.create(_finding())
    approvals._force_state(a.id, approvals.FAILED, detail="领星超时")
    assert _named(doctor.run_checks(), "待审批动作").status == "warn"


def test_doctor_warns_when_write_switch_open(ivyea_home):
    from ivyea_agent import doctor, lingxing_write

    assert _named(doctor.run_checks(), "领星写开关").status == "ok"
    lingxing_write.set_operate(True, ttl_minutes=30)
    c = _named(doctor.run_checks(), "领星写开关")
    assert c.status == "warn" and "分钟后自动关闭" in c.detail


def test_doctor_flags_data_source_failures(ivyea_home):
    from ivyea_agent import doctor, reliability

    assert _named(doctor.run_checks(), "数据源健康").status == "ok"
    reliability.record_failure("patrol.store_l1.1", "领星超时")
    reliability.record_failure("patrol.store_l1.1", "领星超时")
    c = _named(doctor.run_checks(), "数据源健康")
    assert c.status == "warn" and "连续 2 次" in c.detail


def test_doctor_never_raises_on_empty_environment(ivyea_home):
    from ivyea_agent import doctor

    checks = doctor.run_checks()
    assert all(c.status in ("ok", "warn", "fail") for c in checks)
