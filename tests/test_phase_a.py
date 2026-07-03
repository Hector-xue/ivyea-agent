"""Phase A：read_file 行号 + edit 容错、glob、accept-edits、只读命令自动放行。"""
from __future__ import annotations

from ivyea_agent import policy
from ivyea_agent import tools_general as tg
from ivyea_agent.agent_tools import ToolContext, dispatch


def _ctx(tmp_path, allow=()):
    if policy.POLICY_FILE.exists():
        policy.POLICY_FILE.unlink()
    c = ToolContext(workspace=str(tmp_path))
    for k in allow:
        c.perm.session_allow.add(k)
    return c


# ── A1 行号 + edit 容错 ──
def test_read_file_has_line_numbers(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("import os\n\ndef foo():\n    return 1\n", encoding="utf-8")
    out = tg.t_read_file({"path": str(f)}, _ctx(tmp_path))
    assert "\t" in out and "1\timport os" in out
    assert "3\tdef foo():" in out


def test_read_file_range_line_numbers_offset(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")
    out = tg.t_read_file({"path": str(f), "offset": 4, "limit": 2}, _ctx(tmp_path))
    assert "4\tline4" in out and "5\tline5" in out and "line6" not in out


def test_edit_tolerates_pasted_line_numbers(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\n", encoding="utf-8")
    ctx = _ctx(tmp_path, allow=["edit_file"])
    tg.t_read_file({"path": str(f)}, ctx)
    # 模型误把行号粘进 old
    r = tg.t_edit_file({"path": str(f), "old": "   2\tb = 2", "new": "b = 20"}, ctx)
    assert "已编辑" in r and f.read_text() == "a = 1\nb = 20\n"


# ── A2 glob ──
def test_glob_finds_files(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.py").write_text("y", encoding="utf-8")
    (tmp_path / "c.txt").write_text("z", encoding="utf-8")
    out = dispatch("glob", {"pattern": "**/*.py"}, _ctx(tmp_path))
    assert "a.py" in out and "b.py" in out and "c.txt" not in out


# ── A3 accept-edits ──
def test_accept_edits_auto_approves_write(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("v=1", encoding="utf-8")
    ctx = _ctx(tmp_path)
    ctx.perm.accept_edits = True
    tg.t_read_file({"path": str(f)}, ctx)        # 满足改前必读
    r = tg.t_edit_file({"path": str(f), "old": "v=1", "new": "v=2"}, ctx)
    assert "已编辑" in r and f.read_text() == "v=2"


def test_accept_edits_still_blocked_in_plan_mode(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("v=1", encoding="utf-8")
    ctx = _ctx(tmp_path)
    ctx.perm.accept_edits = True
    ctx.plan_mode = True
    tg.t_read_file({"path": str(f)}, ctx)
    r = tg.t_edit_file({"path": str(f), "old": "v=1", "new": "v=2"}, ctx)
    assert "计划模式" in r and f.read_text() == "v=1"


def test_accept_edits_still_requires_prior_read(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("v=1", encoding="utf-8")
    ctx = _ctx(tmp_path)
    ctx.perm.accept_edits = True   # 没读过 → 仍挡回
    r = tg.t_edit_file({"path": str(f), "old": "v=1", "new": "v=2"}, ctx)
    assert "已拦截" in r and f.read_text() == "v=1"


# ── A4 只读命令自动放行 ──
def test_is_readonly_command():
    assert policy.is_readonly_command("ls -la")
    assert policy.is_readonly_command("git status")
    assert policy.is_readonly_command("git diff HEAD~1")
    assert not policy.is_readonly_command("rm -rf x")
    assert not policy.is_readonly_command("echo hi > f")      # 重定向
    assert not policy.is_readonly_command("ls && rm x")        # 串联
    assert not policy.is_readonly_command("cat a | tee b")     # 管道
    assert not policy.is_readonly_command("git commit -m x")   # 写子命令


def test_run_command_readonly_auto_runs(tmp_path):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    ctx = _ctx(tmp_path)   # 没有任何 session_allow / 审批
    out = tg.t_run_command({"command": "ls"}, ctx)
    assert "a.txt" in out and "退出码 0" in out


def test_run_command_write_still_gated_in_plan_mode(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.plan_mode = True
    out = tg.t_run_command({"command": "touch newfile"}, ctx)  # 非只读 → 计划模式拦
    assert "计划模式" in out and not (tmp_path / "newfile").exists()
