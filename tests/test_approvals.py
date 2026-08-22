"""审批状态机测试 —— 安全攸关，重点在「一次性消费」和「非法跃迁」。"""
from __future__ import annotations

import time

import pytest


def _finding(**over):
    from ivyea_agent import store_health

    kw = dict(code="ads.spend_burst", layer="L2", severity="crit",
              action_class=store_health.STANCH, sid=1, scope="campaign",
              target_id="C1", target_name="测试活动", message="预算 100 → 85",
              evidence={"spend": 900.0},
              intent={"op_type": "campaign_budget", "sid": 1, "target_id": "C1",
                      "change": {"daily_budget": 85.0},
                      "before": {"daily_budget": 100.0}})
    kw.update(over)
    return store_health.Finding(**kw)


def test_create_and_get(ivyea_home):
    from ivyea_agent import approvals

    a = approvals.create(_finding(), chat_id="oc_1")
    got = approvals.get(a.id)
    assert got.state == approvals.PENDING
    assert got.intent["change"]["daily_budget"] == 85.0
    assert got.evidence["spend"] == 900.0
    assert got.chat_id == "oc_1"


def test_advisory_finding_cannot_create_approval(ivyea_home):
    """纯告警不该有「批准执行」按钮——点了以为做了事，实际什么也没发生。"""
    from ivyea_agent import approvals

    with pytest.raises(approvals.ApprovalError):
        approvals.create(_finding(intent=None))


def test_approve_is_consumed_exactly_once(ivyea_home):
    """飞书重复点击 / 网络重投都会打到这里，只有第一次能生效。"""
    from ivyea_agent import approvals

    a = approvals.create(_finding())
    ok1, obj1, _ = approvals.resolve(a.id, "approve", operator="ou_me")
    ok2, obj2, why2 = approvals.resolve(a.id, "approve", operator="ou_me")

    assert ok1 and obj1.state == approvals.APPROVED and obj1.resolved_by == "ou_me"
    assert not ok2 and why2 == "already_resolved"
    assert obj2.state == approvals.APPROVED     # 状态没有被第二次点击改动


def test_deny_then_approve_rejected(ivyea_home):
    from ivyea_agent import approvals

    a = approvals.create(_finding())
    approvals.resolve(a.id, "deny", operator="ou_me")
    ok, obj, why = approvals.resolve(a.id, "approve", operator="ou_me")
    assert not ok and why == "already_resolved" and obj.state == approvals.DENIED


def test_unknown_id(ivyea_home):
    from ivyea_agent import approvals

    ok, obj, why = approvals.resolve("nope", "approve")
    assert not ok and obj is None and why == "unknown"


def test_chat_mismatch_rejected(ivyea_home):
    """卡片被转发到别的群，那边一点就执行——必须挡住。"""
    from ivyea_agent import approvals

    a = approvals.create(_finding(), chat_id="oc_original")
    ok, _obj, why = approvals.resolve(a.id, "approve", chat_id="oc_other")
    assert not ok and why == "chat_mismatch"
    assert approvals.get(a.id).state == approvals.PENDING   # 仍可在原会话处理


def test_same_chat_accepted(ivyea_home):
    from ivyea_agent import approvals

    a = approvals.create(_finding(), chat_id="oc_1")
    ok, _o, _w = approvals.resolve(a.id, "approve", chat_id="oc_1")
    assert ok


def test_expired_approval_rejected(ivyea_home):
    """两天前的建议早已不适用当下数据，点了不能执行。"""
    from ivyea_agent import approvals

    a = approvals.create(_finding(), ttl_seconds=-1)
    ok, obj, why = approvals.resolve(a.id, "approve")
    assert not ok and why == "expired"
    assert obj.state == approvals.EXPIRED


def test_expire_due_sweeps_only_pending(ivyea_home):
    from ivyea_agent import approvals

    old = approvals.create(_finding(), ttl_seconds=-1)
    fresh = approvals.create(_finding())
    resolved = approvals.create(_finding(), ttl_seconds=-1)
    approvals._force_state(resolved.id, approvals.EXECUTED)

    assert approvals.expire_due() == 1
    assert approvals.get(old.id).state == approvals.EXPIRED
    assert approvals.get(fresh.id).state == approvals.PENDING
    assert approvals.get(resolved.id).state == approvals.EXECUTED


# ── 非法跃迁 ────────────────────────────────────────────────────────────────
def test_cannot_execute_without_approval(ivyea_home):
    """没批准过的不可能「执行成功」。"""
    from ivyea_agent import approvals

    a = approvals.create(_finding())
    assert approvals.mark_executed(a.id, audit_id="x") is False
    assert approvals.get(a.id).state == approvals.PENDING


def test_execute_after_approve(ivyea_home):
    from ivyea_agent import approvals

    a = approvals.create(_finding())
    approvals.resolve(a.id, "approve")
    assert approvals.mark_executed(a.id, audit_id="aud1", detail="已执行") is True
    got = approvals.get(a.id)
    assert got.state == approvals.EXECUTED and got.audit_id == "aud1"


def test_cannot_execute_twice(ivyea_home):
    from ivyea_agent import approvals

    a = approvals.create(_finding())
    approvals.resolve(a.id, "approve")
    approvals.mark_executed(a.id, audit_id="aud1")
    assert approvals.mark_executed(a.id, audit_id="aud2") is False
    assert approvals.get(a.id).audit_id == "aud1"


def test_rollback_only_after_execution(ivyea_home):
    from ivyea_agent import approvals

    a = approvals.create(_finding())
    assert approvals.mark_rolled_back(a.id) is False
    approvals.resolve(a.id, "approve")
    assert approvals.mark_rolled_back(a.id) is False     # 批准了但还没执行
    approvals.mark_executed(a.id, audit_id="aud1")
    assert approvals.mark_rolled_back(a.id, "已恢复原值") is True
    assert approvals.get(a.id).state == approvals.ROLLED_BACK


def test_failed_path(ivyea_home):
    from ivyea_agent import approvals

    a = approvals.create(_finding())
    approvals.resolve(a.id, "approve")
    assert approvals.mark_failed(a.id, "领星写入失败") is True
    assert approvals.get(a.id).state == approvals.FAILED
    # 失败后不能再标记成功
    assert approvals.mark_executed(a.id, audit_id="x") is False


# ── 查询 ────────────────────────────────────────────────────────────────────
def test_list_and_summary(ivyea_home):
    from ivyea_agent import approvals

    a1 = approvals.create(_finding())
    approvals.create(_finding(target_id="C2"))
    approvals.resolve(a1.id, "deny")

    assert len(approvals.list_items()) == 2
    assert len(approvals.list_items(state=approvals.PENDING)) == 1
    assert approvals.summary() == {approvals.PENDING: 1, approvals.DENIED: 1}
    assert "无审批项" in approvals.render([])


def test_set_message_for_card_update(ivyea_home):
    from ivyea_agent import approvals

    a = approvals.create(_finding())
    assert approvals.set_message(a.id, "oc_1", "om_1") is True
    got = approvals.get(a.id)
    assert (got.chat_id, got.message_id) == ("oc_1", "om_1")


def test_intent_survives_roundtrip_for_write_gate(ivyea_home):
    """存进去再取出来的 intent 必须仍能过 lingxing_write 的幅度硬闸，
    否则用户点了批准会在最后一步被拦。"""
    from ivyea_agent import approvals, lingxing_write

    a = approvals.create(_finding())
    got = approvals.get(a.id)
    ok, why = lingxing_write.magnitude_ok(got.intent)
    assert ok, why


def test_concurrent_clicks_only_one_wins(ivyea_home):
    """选 sqlite 而非 jsonl 的全部理由：并发点击必须只有一次生效。
    8 个线程同时点「批准」，只能有 1 个拿到成功。"""
    import threading

    from ivyea_agent import approvals

    a = approvals.create(_finding())
    results = []
    barrier = threading.Barrier(8)

    def _click():
        barrier.wait()
        ok, _obj, why = approvals.resolve(a.id, "approve", operator="ou_me")
        results.append((ok, why))

    threads = [threading.Thread(target=_click) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [r for r in results if r[0]]
    assert len(wins) == 1, f"应只有一次生效，实际 {len(wins)} 次：{results}"
    assert all(r[1] == "already_resolved" for r in results if not r[0])
    assert approvals.get(a.id).state == approvals.APPROVED
