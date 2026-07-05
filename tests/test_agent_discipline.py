from __future__ import annotations

from ivyea_agent import agent_loop
from ivyea_agent.agent_tools import ToolContext, ToolResult


def _call(call_id: str, name: str, **arguments):
    return {"id": call_id, "name": name, "arguments": arguments}


def test_deadend_forces_list_dir_before_another_search(tmp_path, monkeypatch):
    ctx = ToolContext(workspace=str(tmp_path))
    messages = []
    status = agent_loop.TurnStatus(max_steps=10)

    def fake(name, args, _ctx):
        if name == "grep":
            return ToolResult(True, "⚠ 扫描了 0 个文件（根错误）")
        if name == "list_dir":
            return ToolResult(True, "目录内容")
        raise AssertionError(name)

    monkeypatch.setattr(agent_loop, "dispatch_result", fake)
    agent_loop._dispatch_tool_calls(ctx, messages, status, [_call("g1", "grep", pattern="x")],
                                    0, 10, lambda _s: None)
    assert ctx.search_recovery_required is True
    blocked, _ = agent_loop._run_one(_call("g2", "grep", pattern="y"), ctx)
    assert blocked.ok is False
    assert "先 list_dir" in blocked.text

    agent_loop._dispatch_tool_calls(ctx, messages, status, [_call("l1", "list_dir", path=str(tmp_path))],
                                    1, 10, lambda _s: None)
    assert ctx.search_recovery_required is False


def test_navigation_budget_stops_unbounded_search(tmp_path, monkeypatch):
    ctx = ToolContext(workspace=str(tmp_path), navigation_since_read=8)
    monkeypatch.setattr(agent_loop, "dispatch_result", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must block")))
    result, _ = agent_loop._run_one(_call("g", "glob", pattern="**/*.py"), ctx)
    assert result.ok is False
    assert "已经导航 8 次" in result.text
    assert "read_file" in result.text


def test_successful_read_resets_navigation_and_can_adopt_repo(tmp_path, monkeypatch):
    repo = tmp_path / "ivyea-agent"
    (repo / ".git").mkdir(parents=True)
    target = repo / "main.py"
    target.write_text("print('ok')\n", encoding="utf-8")
    ctx = ToolContext(workspace=str(tmp_path), navigation_since_read=7,
                      consecutive_search_deadends=2, search_recovery_required=True)
    monkeypatch.setattr(agent_loop, "dispatch_result", lambda *_a, **_k: ToolResult(True, "1\tprint('ok')"))
    messages = []
    status = agent_loop.TurnStatus(max_steps=10)
    agent_loop._dispatch_tool_calls(ctx, messages, status, [_call("r", "read_file", path=str(target))],
                                    0, 10, lambda _s: None)
    assert ctx.navigation_since_read == 0
    assert ctx.consecutive_search_deadends == 0
    assert ctx.target_root == str(repo)
    assert "范围已锁定" in messages[-1]["content"]


def test_ambiguous_scope_blocks_search_and_mutation(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), scope_ambiguous=True)
    for name, args in (("grep", {"pattern": "x"}), ("edit_file", {"path": "a"}),
                       ("run_command", {"command": "echo x"})):
        result, _ = agent_loop._run_one({"id": name, "name": name, "arguments": args}, ctx)
        assert result.ok is False
        assert "目标尚未锁定" in result.text


def test_behavioral_write_requires_runtime_validation(monkeypatch):
    ctx = ToolContext(workspace=".", behavioral_task=True)
    status = agent_loop.TurnStatus(max_steps=10, behavioral_task=True, wrote_code=True)
    feedback = agent_loop._verify_gate_feedback(ctx, status, lambda _s: None)
    assert "真实运行路径" in feedback

    status.runtime_validated = True
    monkeypatch.setattr("ivyea_agent.verify.gate", lambda *_a, **_k: {"ok": True})
    assert agent_loop._verify_gate_feedback(ctx, status, lambda _s: None) is None


def test_tests_do_not_count_as_behavior_runtime_evidence():
    status = agent_loop.TurnStatus(max_steps=10, behavioral_task=True)
    status.observe_tool_result("edit_file", ToolResult(True, "已编辑 /tmp/a.py（替换 1 处）"))
    status.observe_tool_result("run_tests", ToolResult(True, "✓ 测试通过"))
    assert status.wrote_code is True
    assert status.runtime_validated is False
    status.observe_tool_result("run_python", ToolResult(True, "[退出码 0]\nok"))
    assert status.runtime_validated is True


def test_new_write_requires_new_behavior_validation():
    status = agent_loop.TurnStatus(max_steps=10, behavioral_task=True)
    status.observe_tool_result("edit_file", ToolResult(True, "已编辑 a.py（替换 1 处）"))
    assert "真实运行路径" in agent_loop._verify_gate_feedback(ToolContext(), status, lambda _s: None)
    status.observe_tool_result("run_python", ToolResult(True, "[退出码 0]\nok"))
    assert status.runtime_validated is True
    status.observe_tool_result("edit_file", ToolResult(True, "已编辑 a.py（替换 1 处）"))
    assert status.runtime_validated is False
    assert "真实运行路径" in agent_loop._verify_gate_feedback(ToolContext(), status, lambda _s: None)
