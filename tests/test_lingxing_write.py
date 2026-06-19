"""领星写入：请求体形状、幅度硬闸、operate 开关、候选转 intent、回滚。

全程 dry-run / monkeypatch，绝不对真实领星发写请求。
"""
from __future__ import annotations

import pytest


def _lw():
    from ivyea_agent import lingxing_write
    return lingxing_write


# ── build_body 黄金形状（逐字段对齐 ivyea-ops）──────────────────────────────
def test_build_body_negate(ivyea_home):
    lw = _lw()
    body = lw.build_body({"op_type": "negate_keyword", "sid": 1876, "campaign_id": "C1",
                          "keyword_text": "junk term", "match_type": "negativeExact"})
    assert body == {"sid": 1876, "negativeKeywords": [
        {"campaignId": "C1", "keyword": "junk term", "matchType": "negativeExact", "state": "ENABLED"}]}


def test_build_body_negate_adgroup(ivyea_home):
    lw = _lw()
    body = lw.build_body({"op_type": "negate_keyword", "sid": 1, "campaign_id": "C1",
                          "ad_group_id": "G9", "keyword_text": "x", "match_type": "negativePhrase"})
    assert body["negativeKeywords"][0]["adGroupId"] == "G9"


def test_build_body_keyword_bid(ivyea_home):
    lw = _lw()
    body = lw.build_body({"op_type": "keyword_bid", "sid": 1876, "target_id": "123",
                          "change": {"bid": 0.85}})
    assert body == {"sid": 1876, "keywords": [{"keywordId": 123, "isBaseValue": 0, "bid": 0.85}]}


def test_build_body_campaign_budget_nested(ivyea_home):
    lw = _lw()
    body = lw.build_body({"op_type": "campaign_budget", "sid": 1876, "target_id": "55",
                          "change": {"daily_budget": 12.0}})
    assert body == {"sid": 1876, "campaigns": [
        {"campaignId": 55, "isBaseValue": 0, "budget": {"budgetType": "DAILY", "budget": 12.0}}]}


# ── 幅度硬闸 ─────────────────────────────────────────────────────────────────
def test_magnitude_over_20pct_blocked(ivyea_home):
    lw = _lw()
    ok, why = lw.magnitude_ok({"op_type": "keyword_bid", "before": {"bid": 1.0}, "change": {"bid": 0.7}})
    assert not ok and "20%" in why


def test_magnitude_within_ok(ivyea_home):
    lw = _lw()
    ok, _ = lw.magnitude_ok({"op_type": "keyword_bid", "before": {"bid": 1.0}, "change": {"bid": 0.85}})
    assert ok


# ── operate 开关（默认关）──────────────────────────────────────────────────────
def test_operate_default_off(ivyea_home):
    lw = _lw()
    assert lw.operate_active() is False


def test_real_write_blocked_when_switch_off(ivyea_home, monkeypatch):
    lw = _lw()
    called = {"n": 0}
    monkeypatch.setattr(lw, "call", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"code": 0})
    r = lw.execute({"op_type": "negate_keyword", "sid": 1, "campaign_id": "C1",
                    "keyword_text": "x", "match_type": "negativeExact"}, dry_run=False)
    assert r["ok"] is False and "operate" in r["detail"]
    assert called["n"] == 0  # 绝不调用真实写接口


def test_dry_run_returns_body_no_call(ivyea_home, monkeypatch):
    lw = _lw()
    monkeypatch.setattr(lw, "call", lambda *a, **k: pytest.fail("dry-run 不应调用 call"))
    r = lw.execute({"op_type": "keyword_bid", "sid": 1, "target_id": "9",
                    "before": {"bid": 1.0}, "change": {"bid": 0.85}}, dry_run=True)
    assert r["ok"] and r["dry_run"] and r["body"]["keywords"][0]["bid"] == 0.85


# ── 候选 → intent ────────────────────────────────────────────────────────────
def test_candidate_to_intent_negate(ivyea_home):
    lw = _lw()
    cand = {"op_type": "negate_keyword", "sid": 1876, "campaign_id": "C1", "target_name": "junk"}
    intent = lw.candidate_to_intent(cand)
    assert intent["keyword_text"] == "junk" and intent["match_type"] == "negativeExact"


def test_candidate_harvest_not_writable(ivyea_home):
    lw = _lw()
    assert lw.candidate_to_intent({"op_type": "add_keyword", "sid": 1}) is None


# ── 执行 + 回滚（monkeypatch，开关开）──────────────────────────────────────────
def test_execute_and_rollback_bid(ivyea_home, monkeypatch):
    lw = _lw()
    lw.set_operate(True)
    sent = []
    monkeypatch.setattr(lw, "call", lambda route, body, **k: sent.append((route, body)) or {"code": 0, "data": {}})
    monkeypatch.setattr(lw, "_current_value", lambda intent: {"bid": 1.0, "state": "enabled"})
    r = lw.execute({"op_type": "keyword_bid", "sid": 1876, "target_id": "123",
                    "target_name": "kw", "before": {"bid": 1.0}, "change": {"bid": 0.85}}, dry_run=False)
    assert r["ok"] and not r["dry_run"] and r["audit_id"]
    assert sent[0][1]["keywords"][0]["bid"] == 0.85
    # 回滚 → 用 snapshot 的旧值 1.0
    rb = lw.rollback(r["audit_id"])
    assert rb["ok"], rb
    assert sent[-1][1]["keywords"][0]["bid"] == 1.0


def test_rollback_negate_uses_archive(ivyea_home, monkeypatch):
    lw = _lw()
    lw.set_operate(True)
    sent = []
    monkeypatch.setattr(lw, "call",
                        lambda route, body, **k: sent.append((route, body)) or {"code": 0, "data": {"success": [{"targetId": "T1"}]}})
    r = lw.execute({"op_type": "negate_keyword", "sid": 1876, "campaign_id": "C1",
                    "keyword_text": "junk", "match_type": "negativeExact"}, dry_run=False)
    assert r["ok"]
    rb = lw.rollback(r["audit_id"])
    assert rb["ok"]
    assert "archiveNegatives" in sent[-1][0] and sent[-1][1]["targetIds"] == ["T1"]
