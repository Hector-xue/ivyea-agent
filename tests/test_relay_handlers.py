"""卡片回调处理测试 —— 不连飞书、不连 agent，全部打桩。"""
import json

import pytest


from ivyea_agent.feishu_relay import agent_client
from ivyea_agent.feishu_relay import gates
from ivyea_agent.feishu_relay import handlers


ME = "ou_me"


@pytest.fixture(autouse=True)
def allow_me(monkeypatch):
    # 打的是取名单的**函数**，不是启动快照常量：判定已经改成每次现取，
    # 只打常量的话这里放行的名单根本不会被用到，而真机上 ~/.ivyea 的
    # 白名单会漏进单测。
    monkeypatch.setattr(gates.config, "allowed_sender_ids", lambda: {ME})
    monkeypatch.setattr(gates.config, "allowed_chat_ids", set)


def _dedup():
    return gates.TokenDedup(ttl=600)


def _value(action="approve", aid="ap1"):
    return {"ivyea_action": action, "approval_id": aid}


# ── 契约 ────────────────────────────────────────────────────────────────────
def test_parse_value_contract():
    assert handlers.parse_value(_value()) == ("approve", "ap1")
    assert handlers.parse_value({"ivyea_action": "rollback", "approval_id": "x"}) \
        == ("rollback", "x")


@pytest.mark.parametrize("bad", [None, "approve", {}, {"ivyea_action": "rm -rf"},
                                 {"approval_id": "x"}])
def test_parse_value_rejects_garbage_without_raising(bad):
    """回调路径上抛异常会让飞书一直重投。"""
    assert handlers.parse_value(bad) == ("", "")


def test_garbage_value_is_ignored(monkeypatch):
    card, note = handlers.handle_card_action(
        value={"foo": "bar"}, operator_open_id=ME, chat_id="oc_1",
        token="t", dedup=_dedup())
    assert card is None and "不合契约" in note


# ── 闸 1 ────────────────────────────────────────────────────────────────────
def test_non_whitelisted_sender_is_dropped(monkeypatch):
    called = []
    monkeypatch.setattr(agent_client, "resolve",
                        lambda *a, **k: called.append(1) or (200, {"ok": True}))
    card, note = handlers.handle_card_action(
        value=_value(), operator_open_id="ou_stranger", chat_id="oc_1",
        token="t", dedup=_dedup())
    assert card is None and "不在白名单" in note
    assert called == [], "白名单没挡住，请求打到 agent 了"


def test_non_whitelisted_chat_is_dropped(monkeypatch):
    monkeypatch.setattr(gates.config, "allowed_chat_ids", lambda: {"oc_ok"})
    called = []
    monkeypatch.setattr(agent_client, "resolve",
                        lambda *a, **k: called.append(1) or (200, {"ok": True}))
    card, note = handlers.handle_card_action(
        value=_value(), operator_open_id=ME, chat_id="oc_evil",
        token="t", dedup=_dedup())
    assert card is None and "不在白名单" in note and called == []


# ── 闸 3 ────────────────────────────────────────────────────────────────────
def test_replayed_token_does_not_reach_agent(monkeypatch):
    """飞书网络抖动会重投；重投若变成第二次执行，就是第二次花钱。"""
    n = {"c": 0}

    def _resolve(*a, **k):
        n["c"] += 1
        return 200, {"ok": True, "state": "executed",
                     "card": {"header": {"title": {"content": "ok"}}}}

    monkeypatch.setattr(agent_client, "resolve", _resolve)
    d = _dedup()
    handlers.handle_card_action(value=_value(), operator_open_id=ME,
                                chat_id="oc_1", token="tok", dedup=d)
    card, note = handlers.handle_card_action(value=_value(), operator_open_id=ME,
                                             chat_id="oc_1", token="tok", dedup=d)
    assert n["c"] == 1, "重投打到 agent 了"
    assert "重投" in note and "处理中" in json.dumps(card, ensure_ascii=False)


# ── 转发与回填 ──────────────────────────────────────────────────────────────
def test_agent_card_is_returned_verbatim(monkeypatch):
    agent_card = {"config": {"wide_screen_mode": True},
                  "header": {"title": {"tag": "plain_text", "content": "✅ 已执行"},
                             "template": "green"}, "elements": []}
    monkeypatch.setattr(agent_client, "resolve",
                        lambda *a, **k: (200, {"ok": True, "state": "executed",
                                               "card": agent_card}))
    card, _note = handlers.handle_card_action(value=_value(), operator_open_id=ME,
                                              chat_id="oc_1", token="t", dedup=_dedup())
    assert card is agent_card, "agent 给了卡片就该原样回填，relay 不该改写"


def test_forwards_operator_and_chat(monkeypatch):
    seen = {}

    def _resolve(aid, choice, operator, chat):
        seen.update(dict(aid=aid, choice=choice, operator=operator, chat=chat))
        return 200, {"ok": True, "card": {"a": 1}}

    monkeypatch.setattr(agent_client, "resolve", _resolve)
    handlers.handle_card_action(value=_value("deny", "ap9"), operator_open_id=ME,
                                chat_id="oc_7", token="t", dedup=_dedup())
    assert seen == {"aid": "ap9", "choice": "deny", "operator": ME, "chat": "oc_7"}


def test_rollback_routes_to_rollback_endpoint(monkeypatch):
    hit = []
    monkeypatch.setattr(agent_client, "rollback",
                        lambda *a, **k: hit.append(1) or (200, {"ok": True, "card": {"x": 1}}))
    monkeypatch.setattr(agent_client, "resolve",
                        lambda *a, **k: pytest.fail("回滚不该走 resolve"))
    handlers.handle_card_action(value=_value("rollback", "ap1"), operator_open_id=ME,
                                chat_id="oc_1", token="t", dedup=_dedup())
    assert hit == [1]


def test_409_without_card_shows_friendly_notice(monkeypatch):
    monkeypatch.setattr(agent_client, "resolve",
                        lambda *a, **k: (409, {"ok": False, "reason": "already_resolved",
                                               "detail": "该操作已处理过（重复点击无效）"}))
    card, note = handlers.handle_card_action(value=_value(), operator_open_id=ME,
                                             chat_id="oc_1", token="t", dedup=_dedup())
    assert "已处理过" in json.dumps(card, ensure_ascii=False)
    assert "409" in note


def test_agent_down_is_surfaced_not_swallowed(monkeypatch):
    """绝不静默吞掉点击——用户必须知道没生效。"""
    def _boom(*a, **k):
        raise agent_client.AgentUnavailable("connection refused")

    monkeypatch.setattr(agent_client, "resolve", _boom)
    card, note = handlers.handle_card_action(value=_value(), operator_open_id=ME,
                                             chat_id="oc_1", token="t", dedup=_dedup())
    dumped = json.dumps(card, ensure_ascii=False)
    assert "后端不可用" in dumped and "connection refused" in dumped
    assert card["header"]["template"] == "red"


def test_detail_action_renders_evidence(monkeypatch):
    monkeypatch.setattr(agent_client, "status",
                        lambda aid: (200, {"preview": "预算 100 → 85", "state": "pending",
                                           "audit_id": "", "evidence": {"spend": 900}}))
    card, _n = handlers.handle_card_action(value=_value("detail", "ap1"),
                                           operator_open_id=ME, chat_id="oc_1",
                                           token="t", dedup=_dedup())
    dumped = json.dumps(card, ensure_ascii=False)
    assert "spend" in dumped and "预算 100 → 85" in dumped


# ── P6：不带 approval_id 的动作 ─────────────────────────────────────────────
def test_approve_all_uses_clicked_card_message_id(monkeypatch):
    """发卡前拿不到 message_id，所以按钮不带它——由回调事件提供。"""
    seen = {}
    monkeypatch.setattr(agent_client, "action",
                        lambda p: seen.update(p) or (200, {"ok": True, "card": {"x": 1}}))
    card, _n = handlers.handle_card_action(
        value={"ivyea_action": "approve_all"}, operator_open_id=ME,
        chat_id="oc_1", token="t", message_id="om_42", dedup=_dedup())
    assert seen["action"] == "approve_all" and seen["message_id"] == "om_42"
    assert card == {"x": 1}


def test_approve_all_without_message_id_is_refused(monkeypatch):
    monkeypatch.setattr(agent_client, "action",
                        lambda p: pytest.fail("不该在缺 message_id 时调用"))
    card, note = handlers.handle_card_action(
        value={"ivyea_action": "approve_all"}, operator_open_id=ME,
        chat_id="oc_1", token="t", message_id="", dedup=_dedup())
    assert "无法定位" in json.dumps(card, ensure_ascii=False)


def test_operate_on_passes_minutes(monkeypatch):
    seen = {}
    monkeypatch.setattr(agent_client, "action",
                        lambda p: seen.update(p) or (200, {"ok": True, "card": {"y": 1}}))
    handlers.handle_card_action(value={"ivyea_action": "operate_on", "minutes": 30},
                                operator_open_id=ME, chat_id="oc_1", token="t",
                                dedup=_dedup())
    assert seen["action"] == "operate_on" and seen["minutes"] == 30


def test_bare_action_still_needs_whitelist(monkeypatch):
    monkeypatch.setattr(agent_client, "action",
                        lambda p: pytest.fail("白名单没挡住批量批准"))
    card, note = handlers.handle_card_action(
        value={"ivyea_action": "approve_all"}, operator_open_id="ou_stranger",
        chat_id="oc_1", token="t", message_id="om_1", dedup=_dedup())
    assert card is None and "不在白名单" in note


def test_single_action_still_requires_approval_id(monkeypatch):
    card, note = handlers.handle_card_action(
        value={"ivyea_action": "approve"}, operator_open_id=ME, chat_id="oc_1",
        token="t", dedup=_dedup())
    assert card is None and "缺 approval_id" in note
