"""审批→执行→回滚 编排测试。安全攸关，逐道闸验证（方案 §6.1 的 4/5/6/7）。"""
from __future__ import annotations

import pytest


def _finding(before=100.0, after=85.0, **over):
    from ivyea_agent import store_health

    kw = dict(code="ads.campaign_out_of_budget", layer="L1", severity="warn",
              action_class=store_health.STANCH, sid=1, scope="campaign",
              target_id="C1", target_name="活动", message=f"预算 {before} → {after}",
              evidence={"x": 1},
              intent={"op_type": "campaign_budget", "sid": 1, "target_id": "C1",
                      "target_name": "活动", "change": {"daily_budget": after},
                      "before": {"daily_budget": before}})
    kw.update(over)
    return store_health.Finding(**kw)


@pytest.fixture()
def appr(ivyea_home, monkeypatch):
    """建一个 pending 审批，并把卡片更新与真实写入都挡在门外。"""
    from ivyea_agent import approval_flow, approvals

    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    a = approvals.create(_finding(), chat_id="oc_1", message_id="om_1")
    return a


def _allow_write(monkeypatch, result=None):
    from ivyea_agent import lingxing_write

    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)
    monkeypatch.setattr(lingxing_write, "execute",
                        lambda intent, dry_run=True: result or
                        {"ok": True, "dry_run": False, "audit_id": "aud1",
                         "detail": "已执行：广告活动·预算 活动：100.0 → 85.0"})


# ── 闸 4：一次性消费 ────────────────────────────────────────────────────────
def test_approve_executes_and_marks_executed(appr, monkeypatch):
    from ivyea_agent import approval_flow, approvals

    _allow_write(monkeypatch)
    r = approval_flow.resolve(appr.id, "approve", operator="ou_me", chat_id="oc_1")
    assert r["ok"] and r["state"] == approvals.EXECUTED and r["audit_id"] == "aud1"
    assert approvals.get(appr.id).state == approvals.EXECUTED
    assert "已执行" in str(r["card"])


def test_second_click_is_rejected(appr, monkeypatch):
    from ivyea_agent import approval_flow

    _allow_write(monkeypatch)
    approval_flow.resolve(appr.id, "approve", chat_id="oc_1")
    r = approval_flow.resolve(appr.id, "approve", chat_id="oc_1")
    assert not r["ok"] and r["reason"] == "already_resolved"
    assert "重复点击" in r["detail"]


def test_deny_does_not_execute(appr, monkeypatch):
    from ivyea_agent import approval_flow, approvals, lingxing_write

    called = []
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)
    monkeypatch.setattr(lingxing_write, "execute",
                        lambda *a, **k: called.append(1) or {"ok": True})
    r = approval_flow.resolve(appr.id, "deny", operator="ou_me", chat_id="oc_1")
    assert r["ok"] and r["state"] == approvals.DENIED and called == []


def test_chat_mismatch_blocks(appr, monkeypatch):
    """卡片被转发到别的会话，那边一点就执行——必须挡住。"""
    from ivyea_agent import approval_flow, approvals

    _allow_write(monkeypatch)
    r = approval_flow.resolve(appr.id, "approve", chat_id="oc_other")
    assert not r["ok"] and r["reason"] == "chat_mismatch"
    assert approvals.get(appr.id).state == approvals.PENDING


def test_expired_blocks(ivyea_home, monkeypatch):
    from ivyea_agent import approval_flow, approvals

    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    a = approvals.create(_finding(), chat_id="oc_1", ttl_seconds=-1)
    _allow_write(monkeypatch)
    r = approval_flow.resolve(a.id, "approve", chat_id="oc_1")
    assert not r["ok"] and r["reason"] == "expired"


# ── 闸 6：幅度硬闸 ──────────────────────────────────────────────────────────
def test_magnitude_gate_blocks_and_marks_failed(ivyea_home, monkeypatch):
    """幅度不合法直接判失败，而不是"等你开了写开关再来撞一次"。"""
    from ivyea_agent import approval_flow, approvals, lingxing_write

    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    a = approvals.create(_finding(before=100.0, after=10.0), chat_id="oc_1")  # -90%
    called = []
    monkeypatch.setattr(lingxing_write, "operate_active",
                        lambda: called.append("switch") or True)
    monkeypatch.setattr(lingxing_write, "execute",
                        lambda *x, **k: called.append("write") or {"ok": True})
    r = approval_flow.resolve(a.id, "approve", chat_id="oc_1")
    assert not r["ok"] and r["reason"] == "guardrail"
    assert approvals.get(a.id).state == approvals.FAILED
    assert "write" not in called, "硬闸没拦住就写出去了"
    assert "switch" not in called, "幅度闸应在写开关之前"


# ── 闸 5：写开关 ────────────────────────────────────────────────────────────
def test_operate_off_keeps_approved_for_retry(appr, monkeypatch):
    """写开关关着时保持 approved：用户的批准意愿仍然有效，
    补开开关后可直接重试，不必重新发一遍卡片。"""
    from ivyea_agent import approval_flow, approvals, lingxing_write

    monkeypatch.setattr(lingxing_write, "operate_active", lambda: False)
    r = approval_flow.resolve(appr.id, "approve", chat_id="oc_1")
    assert not r["ok"] and r["reason"] == "operate_off"
    assert r["state"] == approvals.APPROVED
    assert approvals.get(appr.id).state == approvals.APPROVED

    _allow_write(monkeypatch)                      # 补开开关后重试
    r2 = approval_flow.execute_approved(appr.id, operator="ou_me")
    assert r2["ok"] and approvals.get(appr.id).state == approvals.EXECUTED


def test_execute_requires_approved_state(appr, monkeypatch):
    from ivyea_agent import approval_flow

    _allow_write(monkeypatch)
    r = approval_flow.execute_approved(appr.id)     # 还是 pending
    assert not r["ok"] and r["reason"] == "not_approved"


# ── 闸 7：写失败 / 回滚 ─────────────────────────────────────────────────────
def test_write_failure_marks_failed(appr, monkeypatch):
    from ivyea_agent import approval_flow, approvals, lingxing_write

    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)
    monkeypatch.setattr(lingxing_write, "execute",
                        lambda *a, **k: {"ok": False, "detail": "写入失败已熔断"})
    r = approval_flow.resolve(appr.id, "approve", chat_id="oc_1")
    assert not r["ok"] and r["reason"] == "write_failed"
    assert approvals.get(appr.id).state == approvals.FAILED
    assert "熔断" in str(r["card"])


def test_exception_during_write_is_contained(appr, monkeypatch):
    from ivyea_agent import approval_flow, approvals, lingxing_write

    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("领星超时")

    monkeypatch.setattr(lingxing_write, "execute", _boom)
    r = approval_flow.resolve(appr.id, "approve", chat_id="oc_1")
    assert not r["ok"] and r["reason"] == "exception"
    assert approvals.get(appr.id).state == approvals.FAILED


def test_rollback_happy_path(appr, monkeypatch):
    from ivyea_agent import approval_flow, approvals, lingxing_write

    _allow_write(monkeypatch)
    approval_flow.resolve(appr.id, "approve", chat_id="oc_1")
    monkeypatch.setattr(lingxing_write, "rollback",
                        lambda aid: {"ok": True, "detail": f"已回滚 {aid}"})
    r = approval_flow.rollback(appr.id, operator="ou_me", chat_id="oc_1")
    assert r["ok"] and approvals.get(appr.id).state == approvals.ROLLED_BACK
    assert "已回滚" in str(r["card"])


def test_rollback_requires_executed(appr, monkeypatch):
    from ivyea_agent import approval_flow

    r = approval_flow.rollback(appr.id, chat_id="oc_1")
    assert not r["ok"] and r["reason"] == "not_executed"


def test_rollback_chat_mismatch(appr, monkeypatch):
    from ivyea_agent import approval_flow

    _allow_write(monkeypatch)
    approval_flow.resolve(appr.id, "approve", chat_id="oc_1")
    r = approval_flow.rollback(appr.id, chat_id="oc_other")
    assert not r["ok"] and r["reason"] == "chat_mismatch"


def test_card_update_failure_does_not_fail_the_write(appr, monkeypatch):
    """写已经做了，卡片只是呈现——发不出去不该把成功变成失败。"""
    from ivyea_agent import approval_flow, approvals, feishu_client

    _allow_write(monkeypatch)
    monkeypatch.undo()          # 恢复真实 _update_card

    def _boom(*a, **k):
        raise feishu_client.FeishuError("网络炸了")

    monkeypatch.setattr(feishu_client, "update_card", _boom)
    _allow_write(monkeypatch)
    r = approval_flow.resolve(appr.id, "approve", chat_id="oc_1", update_card=True)
    assert r["ok"] and approvals.get(appr.id).state == approvals.EXECUTED


# ── serve 端点契约（方案 §4.2）──────────────────────────────────────────────
def test_endpoint_resolve_contract(appr, monkeypatch):
    from ivyea_agent import approval_flow, service

    _allow_write(monkeypatch)
    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    code, data = service.feishu_approval_resolve(
        {"approval_id": appr.id, "choice": "approve",
         "operator_open_id": "ou_me", "chat_id": "oc_1"})
    assert code == 200 and data["ok"] and data["state"] == "executed"
    assert "card" in data

    code2, data2 = service.feishu_approval_resolve(
        {"approval_id": appr.id, "choice": "approve", "chat_id": "oc_1"})
    assert code2 == 409 and data2["reason"] == "already_resolved"


@pytest.mark.parametrize("payload", [
    {}, {"approval_id": "x"}, {"choice": "approve"},
    {"approval_id": "x", "choice": "drop_table"},
])
def test_endpoint_rejects_bad_payload(ivyea_home, payload):
    from ivyea_agent import service

    code, data = service.feishu_approval_resolve(payload)
    assert code == 400 and not data["ok"]


def test_endpoint_unknown_id_is_409(ivyea_home):
    from ivyea_agent import service

    code, data = service.feishu_approval_resolve(
        {"approval_id": "nope", "choice": "approve"})
    assert code == 409 and data["reason"] == "unknown"


def test_endpoint_get_status(appr):
    from ivyea_agent import service

    code, data = service.feishu_approval_get(appr.id)
    assert code == 200
    for k in ("id", "state", "intent", "preview", "created_at", "resolved_by", "audit_id"):
        assert k in data, f"契约要求返回 {k}"
    code2, _ = service.feishu_approval_get("nope")
    assert code2 == 404


def test_endpoint_rollback_contract(appr, monkeypatch):
    from ivyea_agent import approval_flow, lingxing_write, service

    _allow_write(monkeypatch)
    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    service.feishu_approval_resolve({"approval_id": appr.id, "choice": "approve",
                                     "chat_id": "oc_1"})
    monkeypatch.setattr(lingxing_write, "rollback",
                        lambda aid: {"ok": True, "detail": "已恢复"})
    code, data = service.feishu_approval_rollback(
        {"approval_id": appr.id, "operator_open_id": "ou_me", "chat_id": "oc_1"})
    assert code == 200 and data["ok"] and "card" in data
