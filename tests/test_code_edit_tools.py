"""Chat-exposed structured code-edit tools (code_apply_patch / run_tests / code_repair)."""
from __future__ import annotations

from ivyea_agent import permission
from ivyea_agent.agent_tools import TOOL_SCHEMAS, ToolContext, dispatch


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    return tmp_path


def test_apply_patch_dry_run_does_not_write(tmp_path):
    root = _repo(tmp_path)
    ctx = ToolContext(workspace=str(root))
    out = dispatch("code_apply_patch", {"ops": [
        {"path": "pkg/calc.py", "old": "return a + b\n", "new": "return a + b + 0\n"}]}, ctx)
    assert "dry" in out.lower() or "校验" in out or "valid" in out.lower()
    # nothing written in dry-run
    assert (root / "pkg" / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")


def test_apply_patch_execute_requires_approval_and_writes(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    # auto-approve so the gate passes in the test
    monkeypatch.setattr(permission, "request_intent", lambda *a, **k: permission.APPROVE)
    ctx = ToolContext(workspace=str(root), execute=True)
    out = dispatch("code_apply_patch", {"ops": [
        {"path": "pkg/calc.py", "old": "return a + b\n", "new": "return a + b + 0\n"}],
        "execute": True}, ctx)
    assert "应用" in out or "applied" in out.lower() or "execute" in out.lower()
    assert (root / "pkg" / "calc.py").read_text(encoding="utf-8").endswith("return a + b + 0\n")


def test_apply_patch_execute_blocked_in_plan_mode(tmp_path):
    root = _repo(tmp_path)
    ctx = ToolContext(workspace=str(root), plan_mode=True)
    out = dispatch("code_apply_patch", {"ops": [
        {"path": "pkg/calc.py", "old": "return a + b\n", "new": "x\n"}], "execute": True}, ctx)
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
