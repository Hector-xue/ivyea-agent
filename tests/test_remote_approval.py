"""远程人在环审批：把 CLI 的 tui 审批卡投影到网页，并保证永远收敛出一个决策。

这条路的核心风险不是"能不能批准"，而是**会不会把 agent 永久挂在一个没人会回的
确认上**。所以超时、页面关闭、乱填选项三条路都有用例钉死，方向一律是拒绝。
"""
from __future__ import annotations

import threading
import time

import pytest


def _options():
    return [("approve", "批准本次"), ("session", "本会话同类都批准"),
            ("deny", "拒绝"), ("abort", "全部停止")]


def test_prompt_blocks_until_decision_arrives():
    """发出 permission_request 后阻塞；决策一到就返回对应选项。"""
    from ivyea_agent import service

    sent: list[tuple[str, dict]] = []
    remote = service.RemoteApproval(lambda ev, data: sent.append((ev, data)), "sid-1")

    result: dict = {}

    def _run():
        result["choice"] = remote.prompt("需要确认写操作", "降低低效目标预算",
                                         _options(), {"op_type": "ops_tool_call"})

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    # 等事件发出来（说明已经在等人了）
    deadline = time.time() + 5
    while not sent and time.time() < deadline:
        time.sleep(0.02)
    assert sent, "没有发出 permission_request"
    event, data = sent[0]
    assert event == "permission_request"
    assert data["session_id"] == "sid-1"
    assert data["op_type"] == "ops_tool_call"
    assert data["preview"] == "降低低效目标预算"
    assert [o["key"] for o in data["options"]] == ["approve", "session", "deny", "abort"]
    request_id = data["request_id"]
    assert request_id in service.pending_permissions()

    # 此刻那一步仍卡着 —— 这正是"没点确认就不落写"的机制本身
    worker.join(timeout=0.3)
    assert worker.is_alive()

    assert service.resolve_permission(request_id, "approve") is True
    worker.join(timeout=5)
    assert result["choice"] == "approve"
    # 收尾必须摘掉登记，否则重启前会一直堆着
    assert request_id not in service.pending_permissions()


def test_timeout_denies_and_notifies():
    """没人理 → 拒绝，并且告诉前端这张卡已经失效。"""
    from ivyea_agent import service

    sent: list[tuple[str, dict]] = []
    remote = service.RemoteApproval(lambda ev, data: sent.append((ev, data)), "sid-2", timeout=0.05)
    choice = remote.prompt("需要确认写操作", "开启领星可写开关", _options(), {})
    assert choice == "deny"
    assert [ev for ev, _ in sent] == ["permission_request", "permission_timeout"]
    assert not service.pending_permissions()


def test_client_disconnect_denies_without_waiting_out_the_timeout():
    """页面关了就没人能确认了，别把这一步在服务端干挂十分钟。"""
    from ivyea_agent import service

    gone = threading.Event()
    gone.set()
    remote = service.RemoteApproval(lambda ev, data: None, "sid-3",
                                    client_gone=gone, timeout=600.0)
    started = time.time()
    assert remote.prompt("需要确认写操作", "改价", _options(), {}) == "deny"
    assert time.time() - started < 5    # 立刻收敛，而不是等满超时


def test_unknown_choice_falls_back_to_deny():
    """前端塞个没发过的选项，不许借此改变语义。"""
    from ivyea_agent import service

    holder: dict = {}
    remote = service.RemoteApproval(lambda ev, data: holder.setdefault("id", data.get("request_id")),
                                    "sid-4", timeout=5.0)
    out: dict = {}
    worker = threading.Thread(
        target=lambda: out.setdefault("choice", remote.prompt("t", "b", _options(), {})),
        daemon=True)
    worker.start()
    deadline = time.time() + 5
    while "id" not in holder and time.time() < deadline:
        time.sleep(0.02)
    assert service.resolve_permission(holder["id"], "yolo") is True
    worker.join(timeout=5)
    assert out["choice"] == "deny"


def test_resolve_unknown_request_id_is_false():
    from ivyea_agent import service
    assert service.resolve_permission("no-such-id", "approve") is False


def test_permission_engine_uses_remote_channel_and_skips_edit():
    """审批引擎认这条通道；远端没法做就地编辑，edit 选项必须先摘掉。"""
    from ivyea_agent import permission

    seen: dict = {}

    def _prompt(title, body, options, meta):
        seen["title"] = title
        seen["options"] = [k for k, _ in options]
        seen["meta"] = meta
        return "deny"

    state = permission.PermissionState(prompt_fn=_prompt)
    decision = permission.request_intent(
        {"op_type": "mcp_call_tool"}, "调用 MCP 工具 lingxing.update_bid", state,
        edit_fn=lambda intent, input_fn: None,   # 有 edit 能力，但远端不该看到它
    )
    assert decision == permission.DENY
    assert "edit" not in seen["options"]
    assert seen["options"] == ["approve", "session", "deny", "abort"]
    assert seen["meta"]["op_type"] == "mcp_call_tool"
    assert seen["meta"]["destructive"] is True


def test_session_choice_allows_same_op_type_without_asking_again():
    """「本会话同类都批准」在远程模式下同样生效，不该每步都再弹一次。"""
    from ivyea_agent import permission

    asked: list[str] = []

    def _prompt(title, body, options, meta):
        asked.append(meta.get("op_type", ""))
        return "session"

    state = permission.PermissionState(prompt_fn=_prompt)
    intent = {"op_type": "ops_tool_call"}
    assert permission.request_intent(intent, "第一次", state) == permission.APPROVE
    assert permission.request_intent(intent, "第二次", state) == permission.APPROVE
    assert asked == ["ops_tool_call"]      # 只问了一次


def test_cli_path_untouched_without_prompt_fn(monkeypatch):
    """没注入 prompt_fn 时仍走 tui.select —— CLI 的交互审批行为一字不变。"""
    from ivyea_agent import permission, tui

    calls: dict = {}

    def _fake_select(title, body, options, kind="warn", input_fn=None):
        calls["kind"] = kind
        calls["options"] = [k for k, _ in options]
        return "approve"

    monkeypatch.setattr(tui, "select", _fake_select)
    state = permission.PermissionState()
    assert permission.request_intent({"op_type": "write_file"}, "写文件", state,
                                     edit_fn=lambda i, f: None) == permission.APPROVE
    assert calls["kind"] == "warn"
    assert "edit" in calls["options"]      # 终端仍然给"改一下"


class _BoardToolProvider:
    """先调一个写类板块能力，再收尾。"""

    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "final", "content": "", "usage": {}, "tool_calls": [{
                "id": "b1", "name": "ivyea_ops_call_tool",
                "arguments": {"name": "lingxing_operate_enable", "arguments": {}},
            }]}
        else:
            yield {"type": "final", "content": "好了", "tool_calls": [], "usage": {}}


@pytest.mark.parametrize("decision,expect_called", [("approve", True), ("deny", False)])
def test_destructive_board_tool_requires_approval(ivyea_home, monkeypatch, decision, expect_called):
    """写类板块能力（开领星可写开关这种）必须先问过人，拒绝就真的不调。"""
    from ivyea_agent import agent_loop, agent_tools, permission

    called: list[str] = []

    def _fake_bridge(ctx, path, payload, timeout=80.0):
        if path == "/tools":
            return {"ok": True, "tools": [
                {"name": "lingxing_operate_enable", "title": "开启领星操作开关",
                 "module": "admin", "destructive": True},
                {"name": "market_history", "title": "市场调研历史",
                 "module": "market", "destructive": False},
            ]}
        called.append(str(payload.get("name")))
        return {"ok": True, "result": "done"}

    monkeypatch.setattr(agent_tools, "_ops_bridge_request", _fake_bridge)

    ctx = agent_tools.ToolContext(session_id="sid-board")
    ctx.ops_bridge = {"base_url": "http://x/api", "token": "t"}
    ctx.perm.prompt_fn = lambda title, body, options, meta: decision

    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "开一下写开关"}]
    agent_loop.run_turn_stream(_BoardToolProvider(), ctx, msgs,
                               render=lambda s: None, narrate=lambda s: None)
    assert (called == ["lingxing_operate_enable"]) is expect_called
    assert permission  # 引擎确实参与了这条路径


def test_readonly_board_tool_is_not_gated(ivyea_home, monkeypatch):
    """只读板块能力不该被审批打断 —— 否则每查一次历史都要点一次确认。"""
    from ivyea_agent import agent_tools

    def _fake_bridge(ctx, path, payload, timeout=80.0):
        if path == "/tools":
            return {"ok": True, "tools": [
                {"name": "market_history", "title": "市场调研历史", "destructive": False}]}
        return {"ok": True, "result": "rows"}

    monkeypatch.setattr(agent_tools, "_ops_bridge_request", _fake_bridge)
    ctx = agent_tools.ToolContext()
    ctx.ops_bridge = {"base_url": "http://x/api", "token": "t"}
    asked: list[str] = []
    ctx.perm.prompt_fn = lambda *a, **k: asked.append("asked") or "deny"

    out = agent_tools._t_ivyea_ops_call_tool({"name": "market_history", "arguments": {}}, ctx)
    assert "rows" in out
    assert asked == []


def test_board_tool_unchanged_when_no_approval_channel(ivyea_home, monkeypatch):
    """没有审批通道时保持既有行为：嵌入式对话一直这么跑的，这次不改它。"""
    from ivyea_agent import agent_tools

    called: list[str] = []

    def _fake_bridge(ctx, path, payload, timeout=80.0):
        if path == "/tools":
            raise AssertionError("没有审批通道时不该去查目录")
        called.append(str(payload.get("name")))
        return {"ok": True, "result": "done"}

    monkeypatch.setattr(agent_tools, "_ops_bridge_request", _fake_bridge)
    ctx = agent_tools.ToolContext()            # perm.prompt_fn 为 None
    ctx.ops_bridge = {"base_url": "http://x/api", "token": "t"}
    agent_tools._t_ivyea_ops_call_tool({"name": "lingxing_operate_enable", "arguments": {}}, ctx)
    assert called == ["lingxing_operate_enable"]
