"""证据台账：只记有验证意义的东西，跨轮可查，坏了不连累干活。"""
from __future__ import annotations

from ivyea_agent import agent_loop, evidence_ledger
from ivyea_agent.agent_tools import ToolContext, ToolResult


def test_only_verification_shaped_tools_are_recorded(ivyea_home):
    evidence_ledger.record_tool("s", "t", "run_command", {"command": "pytest -q"}, True,
                                "[退出码 0]\nok")
    evidence_ledger.record_tool("s", "t", "grep", {"pattern": "x"}, True, "命中 3 处")
    evidence_ledger.record_tool("s", "t", "list_dir", {"path": "."}, True, "3 项")
    evidence_ledger.record_tool("s", "t", "todo_write", {"todos": []}, True, "已更新计划")
    rows = evidence_ledger.rows(session_id="s")
    assert [r["kind"] for r in rows] == ["command"]


def test_exit_code_is_extracted(ivyea_home):
    """**只用代码库里真实存在的格式**。此前这里有一条 `已结束（exit=137）` ——
    那个字符串本仓一次都没出现过，测的是我自己编的格式，等于什么也没测到。"""
    for text, want in (("[退出码 0]\nok", "退出码 0"),                    # run_command / run_python
                       ("b1 已结束（退出码 137）", "退出码 137"),           # bash_output 收尾
                       ("ok=False returncode=1", "退出码 1")):            # self_manage
        evidence_ledger.record_tool("x", "t", "run_command", {"command": "c"}, True, text)
    assert [r["detail"] for r in evidence_ledger.rows(session_id="x")] == \
        ["退出码 0", "退出码 137", "退出码 1"]


def test_the_exit_code_formats_actually_exist_in_the_codebase():
    """守住这次教训：正则里的每种格式，代码库里都得真有人在产出它。"""
    import pathlib as _p
    src = "\n".join(f.read_text(encoding="utf-8")
                    for f in _p.Path("ivyea_agent").rglob("*.py")
                    if f.name not in ("evidence_ledger.py", "agent_loop.py"))
    assert "退出码 " in src
    assert "returncode=" in src
    assert "（exit=" not in src        # 编出来的那个格式，别再溜回来


def test_blocked_and_rejected_calls_are_not_evidence(ivyea_home):
    """被护栏拦下、被工具拒绝的调用是待办，不是证明。"""
    evidence_ledger.record_tool("s2", "t", "run_command", {"command": "c"}, False,
                                "已拦截：当前任务同时指向多个项目")
    evidence_ledger.record_tool("s2", "t", "run_tests", {}, False, "⚠ 测试命令不存在")
    assert evidence_ledger.rows(session_id="s2") == []


def test_a_genuine_failure_is_still_evidence(ivyea_home):
    """跑了但没跑通，也是"我们试过了"的证据 —— 只是标成失败。"""
    evidence_ledger.record_tool("s3", "t", "run_tests", {"command": "pytest"}, False,
                                "[退出码 1]\n2 failed")
    row = evidence_ledger.rows(session_id="s3")[0]
    assert row["ok"] == 0 and row["detail"] == "退出码 1"
    assert evidence_ledger.render(session_id="s3", ok_only=False)[0].startswith("跑了测试")


def test_multi_file_patch_records_every_path(ivyea_home):
    evidence_ledger.record_tool("s4", "t", "code_apply_patch",
                                {"ops": [{"path": "a.py"}, {"path": "b/c.ts"}]}, True, "已应用补丁")
    assert "a.py" in evidence_ledger.rows(session_id="s4")[0]["target"]
    assert "b/c.ts" in evidence_ledger.rows(session_id="s4")[0]["target"]


def test_turn_scoping(ivyea_home):
    evidence_ledger.record_tool("s5", "t1", "run_command", {"command": "第一轮"}, True, "退出码 0")
    evidence_ledger.record_tool("s5", "t2", "run_command", {"command": "第二轮"}, True, "退出码 0")
    assert len(evidence_ledger.rows(session_id="s5")) == 2
    assert len(evidence_ledger.rows(session_id="s5", turn_id="t2")) == 1
    assert "第二轮" in evidence_ledger.render(session_id="s5", turn_id="t2")[0]


def test_has_verification(ivyea_home):
    assert evidence_ledger.has_verification(session_id="s6") is False
    evidence_ledger.record_tool("s6", "t", "read_file", {"path": "a.py"}, True, "内容")
    assert evidence_ledger.has_verification(session_id="s6") is False   # 读文件不算"跑通了"
    evidence_ledger.record_tool("s6", "t", "run_command", {"command": "c"}, True, "退出码 0")
    assert evidence_ledger.has_verification(session_id="s6") is True


def test_secrets_are_redacted(ivyea_home):
    evidence_ledger.record_tool("s7", "t", "run_command",
                                {"command": "curl -H 'Authorization: Bearer sk-abcdef1234567890'"},
                                True, "退出码 0")
    assert "sk-abcdef1234567890" not in evidence_ledger.rows(session_id="s7")[0]["target"]


def test_a_broken_ledger_never_breaks_a_tool_call(ivyea_home, monkeypatch):
    monkeypatch.setattr(evidence_ledger, "_conn",
                        lambda: (_ for _ in ()).throw(__import__("sqlite3").OperationalError("locked")))
    evidence_ledger.record_tool("s8", "t", "run_command", {"command": "c"}, True, "退出码 0")
    assert evidence_ledger.rows(session_id="s8") == []


# ── 接线 ─────────────────────────────────────────────────────────────────────
def test_the_turn_loop_feeds_the_ledger(ivyea_home):
    ctx = ToolContext(workspace=".", session_id="wire-1", turn_id="t1")
    agent_loop._record_tool_result(
        ctx, [], {"id": "1", "name": "run_command", "arguments": {"command": "echo hi"}},
        ToolResult(True, "[退出码 0]\nhi"), 12, lambda _s: None)
    assert evidence_ledger.render(session_id="wire-1")[0].startswith("跑了命令 echo hi")


def test_blocked_results_do_not_reach_the_ledger(ivyea_home):
    ctx = ToolContext(workspace=".", session_id="wire-2", turn_id="t1")
    agent_loop._record_tool_result(
        ctx, [], {"id": "1", "name": "run_command", "arguments": {"command": "echo hi"}},
        ToolResult(False, "已拦截：目标尚未锁定"), 1, lambda _s: None, blocked=True)
    assert evidence_ledger.rows(session_id="wire-2") == []


def test_final_report_evidence_falls_back_to_the_ledger(ivyea_home):
    """模型和阶段报告都没给证据时，汇报的「验证」一栏也不能是空口。"""
    from ivyea_agent import progress_reporting

    ctx = ToolContext(workspace=".", session_id="wire-3", turn_id="t1")
    ctx.todos = [{"content": "跑通", "status": "completed"}]
    ctx.progress_started = True
    ctx.progress_active_phase = 0
    evidence_ledger.record_tool("wire-3", "t1", "run_tests", {"command": "pytest -q"}, True,
                                "[退出码 0]\n全过")
    out = progress_reporting.apply_update({"kind": "final", "summary": "做完了"}, ctx)
    assert out["ok"] is True
    assert any("pytest -q" in e for e in out["event"]["evidence"])
