"""P6 打磨：阈值可调 / 批量批准 / 写开关按钮。"""
from __future__ import annotations

import pytest


def _finding(before=100.0, after=85.0, target="C1", **over):
    from ivyea_agent import store_health

    kw = dict(code="ads.campaign_out_of_budget", layer="L1", severity="warn",
              action_class=store_health.STANCH, sid=1, scope="campaign",
              target_id=target, target_name=f"活动{target}",
              message=f"预算 {before} → {after}", evidence={},
              intent={"op_type": "campaign_budget", "sid": 1, "target_id": target,
                      "change": {"daily_budget": after},
                      "before": {"daily_budget": before}})
    kw.update(over)
    return store_health.Finding(**kw)


# ── 阈值 ────────────────────────────────────────────────────────────────────
def test_threshold_override_and_reset(ivyea_home):
    from ivyea_agent import store_health as sh

    assert sh.threshold("stock.days_low.days") == sh.THRESHOLDS["stock.days_low.days"]
    sh.set_threshold("stock.days_low.days", 21)
    assert sh.threshold("stock.days_low.days") == 21.0
    sh.reset_threshold("stock.days_low.days")
    assert sh.threshold("stock.days_low.days") == sh.THRESHOLDS["stock.days_low.days"]


def test_unknown_threshold_key_raises(ivyea_home):
    """手滑写错键名必须报错，不能静默用默认值还以为改成功了。"""
    from ivyea_agent import store_health as sh

    with pytest.raises(KeyError):
        sh.threshold("nope.nope")
    with pytest.raises(KeyError):
        sh.set_threshold("nope.nope", 1)


def test_threshold_type_is_coerced_and_validated(ivyea_home):
    from ivyea_agent import store_health as sh

    assert sh.set_threshold("stock.days_low.days", "18") == 18.0     # 飞书传来的是字符串
    with pytest.raises(ValueError):
        sh.set_threshold("stock.days_low.days", "很多")


def test_threshold_change_takes_effect_in_rules(ivyea_home, monkeypatch):
    """改完立刻生效——不用改代码重启。"""
    from ivyea_agent import metrics, datasources, store_health as sh
    from ivyea_agent.datasources.lingxing_source import LingxingSource

    class _S:
        name, label = "fake", "t"
        def supports(self, m): return m == "inventory.fba_snapshot"
        def lag_seconds(self, m): return 0.0
        def fetch(self, m, scope, window=None):
            return [LingxingSource._inventory(
                {"msku": "A", "fulfillment_channel_name": "FBA",
                 "afn_fulfillable_quantity": 10,
                 "historical_days_of_supply": "20.00"}, 1)]

    for s in list(metrics.registered()):
        metrics.unregister(s.name)
    metrics.register(_S(), priority=1)
    monkeypatch.setattr(datasources, "install_defaults", lambda: None)

    assert "stock.days_low" not in [f.code for f in sh.check_l1(1).findings]  # 20 > 14
    sh.set_threshold("stock.days_low.days", 30)
    assert "stock.days_low" in [f.code for f in sh.check_l1(1).findings]      # 20 < 30
    for s in list(metrics.registered()):
        metrics.unregister(s.name)


def test_threshold_table_marks_overrides(ivyea_home):
    from ivyea_agent import store_health as sh

    sh.set_threshold("ads.cpc_jump.pct", 0.5)
    rows = {r["key"]: r for r in sh.threshold_table()}
    assert rows["ads.cpc_jump.pct"]["overridden"] is True
    assert rows["ads.cpc_jump.pct"]["current"] == 0.5
    assert rows["stock.excess.min_qty"]["overridden"] is False


# ── 批量批准 ────────────────────────────────────────────────────────────────
@pytest.fixture()
def batch(ivyea_home, monkeypatch):
    from ivyea_agent import approval_flow, approvals

    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    ids = []
    for i in range(3):
        a = approvals.create(_finding(target=f"C{i}"), chat_id="oc_1", message_id="om_1")
        ids.append(a.id)
    approvals.create(_finding(target="C9"), chat_id="oc_1", message_id="om_OTHER")
    return ids


def test_approve_all_requires_confirmation(batch, monkeypatch):
    """一次点击执行 N 个写操作，风险与收益不对称，必须二次确认。"""
    from ivyea_agent import approval_flow, approvals, lingxing_write

    called = []
    monkeypatch.setattr(lingxing_write, "operate_active",
                        lambda: called.append(1) or True)
    r = approval_flow.approve_all("om_1", operator="ou_me", chat_id="oc_1")
    assert r["ok"] and r["reason"] == "need_confirm" and r["count"] == 3
    assert called == [], "第一次点击就执行了"
    assert approvals.summary().get(approvals.PENDING) == 4     # 一个都没动

    import json
    btn = [e for e in r["card"]["elements"] if e.get("tag") == "action"][0]["actions"][0]
    assert btn["value"]["ivyea_action"] == "approve_all_confirm"
    assert "3" in json.dumps(r["card"], ensure_ascii=False)


def test_approve_all_confirm_executes_only_that_card(batch, monkeypatch):
    from ivyea_agent import approval_flow, approvals, lingxing_write

    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)
    monkeypatch.setattr(lingxing_write, "execute",
                        lambda i, dry_run=True: {"ok": True, "audit_id": "aud",
                                                 "detail": "已执行"})
    r = approval_flow.approve_all("om_1", operator="ou_me", chat_id="oc_1", confirm=True)
    assert r["ok"] and r["count"] == 3 and r["done"] == 3
    s = approvals.summary()
    assert s.get(approvals.EXECUTED) == 3
    assert s.get(approvals.PENDING) == 1, "动到了别的卡片上的审批项"


def test_approve_all_reports_partial_failure(batch, monkeypatch):
    from ivyea_agent import approval_flow, lingxing_write

    seq = {"n": 0}

    def _exec(intent, dry_run=True):
        seq["n"] += 1
        if seq["n"] == 2:
            return {"ok": False, "detail": "领星超时"}
        return {"ok": True, "audit_id": f"aud{seq['n']}", "detail": "已执行"}

    monkeypatch.setattr(lingxing_write, "operate_active", lambda: True)
    monkeypatch.setattr(lingxing_write, "execute", _exec)
    r = approval_flow.approve_all("om_1", chat_id="oc_1", confirm=True)
    assert r["done"] == 2 and r["count"] == 3
    assert "领星超时" in str(r["card"])


def test_approve_all_on_empty_card(ivyea_home):
    from ivyea_agent import approval_flow

    r = approval_flow.approve_all("om_nothing")
    assert not r["ok"] and r["reason"] == "nothing_pending"


def test_approve_all_respects_chat_scope(batch, monkeypatch):
    from ivyea_agent import approval_flow

    r = approval_flow.approve_all("om_1", chat_id="oc_other")
    assert not r["ok"] and r["reason"] == "nothing_pending"


# ── 写开关按钮 ──────────────────────────────────────────────────────────────
def test_operate_on_sets_switch_with_ttl(ivyea_home):
    from ivyea_agent import approval_flow, lingxing_write

    assert lingxing_write.operate_active() is False
    r = approval_flow.set_operate(minutes=30, operator="ou_me")
    assert r["ok"] and r["expires_in_minutes"] == 30
    assert lingxing_write.operate_active() is True
    assert approval_flow.operate_status()["active"] is True


def test_operate_minutes_are_clamped(ivyea_home):
    from ivyea_agent import approval_flow

    assert approval_flow.set_operate(minutes=99999)["expires_in_minutes"] == 480
    assert approval_flow.set_operate(minutes=0)["expires_in_minutes"] == 120


def test_operate_off_card_offers_the_button(ivyea_home, monkeypatch):
    """被"写开关未开"挡住时，出路不该是"你去登服务器敲命令"。"""
    import json

    from ivyea_agent import approval_flow, approvals, lingxing_write

    monkeypatch.setattr(approval_flow, "_update_card", lambda a, c: True)
    monkeypatch.setattr(lingxing_write, "operate_active", lambda: False)
    a = approvals.create(_finding(), chat_id="oc_1")
    r = approval_flow.resolve(a.id, "approve", chat_id="oc_1")
    dumped = json.dumps(r["card"], ensure_ascii=False)
    assert "operate_on" in dumped and "开启写开关" in dumped


# ── 端点 ────────────────────────────────────────────────────────────────────
def test_action_endpoint_contract(batch, monkeypatch):
    from ivyea_agent import service

    code, data = service.feishu_action({"action": "approve_all", "message_id": "om_1",
                                        "chat_id": "oc_1"})
    assert code == 200 and data["reason"] == "need_confirm"

    code2, data2 = service.feishu_action({"action": "approve_all"})
    assert code2 == 400

    code3, data3 = service.feishu_action({"action": "nope"})
    assert code3 == 400 and "未知动作" in data3["error"]


def test_action_endpoint_threshold(ivyea_home):
    from ivyea_agent import service

    code, data = service.feishu_action({"action": "threshold_list"})
    assert code == 200 and data["thresholds"]

    code2, data2 = service.feishu_action({"action": "threshold_set",
                                          "key": "ads.cpc_jump.pct", "value": "0.4"})
    assert code2 == 200 and data2["value"] == 0.4

    code3, _ = service.feishu_action({"action": "threshold_set", "key": "nope",
                                      "value": 1})
    assert code3 == 404
    code4, _ = service.feishu_action({"action": "threshold_set",
                                      "key": "ads.cpc_jump.pct", "value": "多"})
    assert code4 == 400


def test_action_endpoint_operate(ivyea_home):
    from ivyea_agent import service

    code, data = service.feishu_action({"action": "operate_on", "minutes": 15})
    assert code == 200 and data["expires_in_minutes"] == 15
    code2, data2 = service.feishu_action({"action": "operate_status"})
    assert code2 == 200 and data2["active"] is True
