"""巡检 → 卡片 → 审批项 的粘合测试。"""
from __future__ import annotations

import pytest


def _finding(code="ads.campaign_out_of_budget", intent=True, **over):
    from ivyea_agent import store_health

    kw = dict(code=code, layer="L1", severity="warn",
              action_class=store_health.STANCH, sid=1, scope="campaign",
              target_id="C1", target_name="活动", message="预算耗尽",
              evidence={"x": 1},
              intent=({"op_type": "campaign_budget", "sid": 1, "target_id": "C1",
                       "change": {"daily_budget": 85.0},
                       "before": {"daily_budget": 100.0}} if intent else None))
    kw.update(over)
    return store_health.Finding(**kw)


@pytest.fixture()
def sink(ivyea_home, monkeypatch):
    from ivyea_agent import notify

    box = {"cards": []}

    def _send_card(card, *, chat_id="", extra_parts=(), title=""):
        box["cards"].append(card)
        return {"ok": True, "channel": "feishu_app",
                "message_id": f"om_{len(box['cards'])}", "chat_id": chat_id or "oc_d"}

    monkeypatch.setattr(notify, "send_card", _send_card)
    return box


def _result(*findings):
    from ivyea_agent import store_health

    r = store_health.CheckResult(sid=1, layer="L1")
    r.findings.extend(findings)
    return r


def test_only_actionable_findings_get_approvals(sink):
    from ivyea_agent import approvals, patrol_push

    out = patrol_push.push_result(
        _result(_finding(), _finding(code="stock.oos", intent=False, target_id="M1")),
        chat_id="oc_d")
    assert out["ok"] and out["findings"] == 2
    assert len(out["approvals"]) == 1
    assert approvals.summary().get(approvals.PENDING) == 1


def test_message_id_is_written_back_for_card_update(sink):
    """点按钮后要原地改卡；拿不到 message_id 就改不了。"""
    from ivyea_agent import approvals, patrol_push

    out = patrol_push.push_result(_result(_finding()), chat_id="oc_d")
    a = approvals.get(out["approvals"][0])
    assert a.message_id == out["message_id"] and a.chat_id == "oc_d"


def test_approval_ids_land_on_card_buttons(sink):
    import json

    from ivyea_agent import patrol_push

    out = patrol_push.push_result(_result(_finding()), chat_id="oc_d")
    dumped = json.dumps(sink["cards"][0], ensure_ascii=False)
    assert out["approvals"][0] in dumped


def test_advisory_only_result_creates_no_approvals(sink):
    from ivyea_agent import approvals, patrol_push

    out = patrol_push.push_result(
        _result(_finding(code="stock.oos", intent=False)), chat_id="oc_d")
    assert out["approvals"] == []
    assert approvals.summary() == {}


def test_send_failure_does_not_write_message_id(ivyea_home, monkeypatch):
    from ivyea_agent import approvals, notify, patrol_push

    monkeypatch.setattr(notify, "send_card",
                        lambda *a, **k: {"ok": False, "error": "bot not in chat"})
    out = patrol_push.push_result(_result(_finding()), chat_id="oc_d")
    assert not out["ok"] and out["error"] == "bot not in chat"
    a = approvals.get(out["approvals"][0])
    assert a.message_id == ""      # 没发出去就不能记 message_id


def test_daily_push_includes_gaps(sink):
    import json

    from ivyea_agent import patrol_push, store_health

    r = store_health.CheckResult(sid=1, layer="L3")
    r.gaps.append("profit.asin 无数据源")
    out = patrol_push.push_daily(r, date="2026-08-22", store_name="UK",
                                 metrics_lines=["销售额 100"], chat_id="oc_d")
    assert out["ok"]
    assert "profit.asin 无数据源" in json.dumps(sink["cards"][0], ensure_ascii=False)
