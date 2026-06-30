"""Chat-exposed structured code-edit tools (code_apply_patch / run_tests / code_repair)."""
from __future__ import annotations

from ivyea_agent import permission
from ivyea_agent.agent_tools import TOOL_SCHEMAS, ToolContext, dispatch


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    return tmp_path


def test_apply_patch_denied_does_not_write(tmp_path, monkeypatch):
    # 一次性语义：先校验通过 → 弹审批；用户拒绝则不落盘。
    root = _repo(tmp_path)
    monkeypatch.setattr(permission, "request_intent", lambda *a, **k: permission.DENY)
    ctx = ToolContext(workspace=str(root))
    out = dispatch("code_apply_patch", {"ops": [
        {"path": "pkg/calc.py", "old": "return a + b\n", "new": "return a + b + 0\n"}]}, ctx)
    assert "跳过" in out or "deny" in out.lower()
    assert (root / "pkg" / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")


def test_apply_patch_approve_writes(tmp_path, monkeypatch):
    # 无需 execute 两步：一次审批即落盘。
    root = _repo(tmp_path)
    monkeypatch.setattr(permission, "request_intent", lambda *a, **k: permission.APPROVE)
    ctx = ToolContext(workspace=str(root), execute=True)
    out = dispatch("code_apply_patch", {"ops": [
        {"path": "pkg/calc.py", "old": "return a + b\n", "new": "return a + b + 0\n"}]}, ctx)
    assert "应用" in out or "applied" in out.lower() or "execute" in out.lower()
    assert (root / "pkg" / "calc.py").read_text(encoding="utf-8").endswith("return a + b + 0\n")


def test_apply_patch_invalid_returns_without_approval(tmp_path, monkeypatch):
    # 校验失败（old 不匹配）直接返回，不应弹审批、不落盘。
    root = _repo(tmp_path)
    def _boom(*a, **k):
        raise AssertionError("校验失败时不应请求审批")
    monkeypatch.setattr(permission, "request_intent", _boom)
    ctx = ToolContext(workspace=str(root))
    out = dispatch("code_apply_patch", {"ops": [
        {"path": "pkg/calc.py", "old": "不存在的原文\n", "new": "x\n"}]}, ctx)
    assert (root / "pkg" / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")
    assert out  # 返回了校验诊断


def test_apply_patch_blocked_in_plan_mode(tmp_path):
    root = _repo(tmp_path)
    ctx = ToolContext(workspace=str(root), plan_mode=True)
    out = dispatch("code_apply_patch", {"ops": [
        {"path": "pkg/calc.py", "old": "return a + b\n", "new": "x\n"}]}, ctx)
    assert "计划模式" in out
    assert (root / "pkg" / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")


def test_apply_patch_empty_ops(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path))
    assert "ops 为空" in dispatch("code_apply_patch", {"ops": []}, ctx)


def test_code_repair_parses_failure(tmp_path):
    ctx = ToolContext(workspace=str(_repo(tmp_path)))
    out = dispatch("code_repair", {"test_output":
        "FAILED tests/test_calc.py::test_add - AssertionError: expected sum"}, ctx)
    assert "tests/test_calc.py" in out


def test_edit_tools_registered():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert {"code_apply_patch", "run_tests", "code_repair"} <= names
