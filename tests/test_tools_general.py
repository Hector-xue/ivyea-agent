"""通用工具：读自动放行、写/执行门控、plan 模式拦截、沙箱、web_fetch。

门控的「放行」路径用 ctx.perm.session_allow 预置（免 stdin）；「拦截」用 plan_mode。
"""
from __future__ import annotations

from ivyea_agent import tools_general as tg
from ivyea_agent.agent_tools import ToolContext


def _ctx(tmp_path, allow=()):
    from ivyea_agent import policy
    if policy.POLICY_FILE.exists():
        policy.POLICY_FILE.unlink()
    c = ToolContext(workspace=str(tmp_path))
    for k in allow:
        c.perm.session_allow.add(k)
    return c


# ── 读类（自动放行）──
def test_read_and_list(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("你好 ivyea", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert "你好 ivyea" in tg.t_read_file({"path": str(f)}, ctx)
    out = tg.t_list_dir({"path": str(tmp_path)}, ctx)
    assert "a.txt" in out


def test_read_missing(tmp_path):
    assert "不存在" in tg.t_read_file({"path": str(tmp_path / "nope")}, _ctx(tmp_path))


# ── write_file 门控 ──
def test_write_approved(tmp_path):
    ctx = _ctx(tmp_path, allow=["write_file"])
    f = tmp_path / "out.txt"
    r = tg.t_write_file({"path": str(f), "content": "data"}, ctx)
    assert "已写入" in r and f.read_text() == "data"


def test_write_blocked_in_plan_mode(tmp_path):
    ctx = _ctx(tmp_path, allow=["write_file"])
    ctx.plan_mode = True
    f = tmp_path / "out.txt"
    r = tg.t_write_file({"path": str(f), "content": "data"}, ctx)
    assert "计划模式" in r and not f.exists()


# ── 后台 bash（Phase 1）──
def test_run_command_background_and_output(tmp_path):
    import re
    import time
    ctx = _ctx(tmp_path, allow=["run_command"])
    r = tg.t_run_command({"command": "echo hello-bg", "run_in_background": True}, ctx)
    assert "bash_id=" in r
    bid = re.search(r"bash_id=(bg-\d+)", r).group(1)
    out = ""
    for _ in range(50):
        out = tg.t_bash_output({"bash_id": bid}, ctx)
        if "已结束" in out:
            break
        time.sleep(0.1)
    assert "hello-bg" in out and "已结束" in out       # 轮询到输出 + 退出状态


def test_kill_bash(tmp_path):
    import re
    ctx = _ctx(tmp_path, allow=["run_command"])
    r = tg.t_run_command({"command": "sleep 30", "run_in_background": True}, ctx)
    bid = re.search(r"bash_id=(bg-\d+)", r).group(1)
    assert "已终止" in tg.t_kill_bash({"bash_id": bid}, ctx)
    assert "没有该后台任务" in tg.t_bash_output({"bash_id": "bg-9999"}, ctx)


# ── edit_file ──
def test_edit_unique(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("bid 1.00 done", encoding="utf-8")
    ctx = _ctx(tmp_path, allow=["edit_file"])
    tg.t_read_file({"path": str(f)}, ctx)   # 改前必读硬护栏：先读再编辑
    r = tg.t_edit_file({"path": str(f), "old": "1.00", "new": "0.85"}, ctx)
    assert "已编辑" in r and f.read_text() == "bid 0.85 done"


def test_edit_blocked_without_prior_read(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("bid 1.00 done", encoding="utf-8")
    ctx = _ctx(tmp_path, allow=["edit_file"])
    r = tg.t_edit_file({"path": str(f), "old": "1.00", "new": "0.85"}, ctx)
    assert "已拦截" in r and "read_file" in r and f.read_text() == "bid 1.00 done"  # 未读→挡回，未改


def test_edit_nonunique_refused(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("x x", encoding="utf-8")
    ctx = _ctx(tmp_path, allow=["edit_file"])
    r = tg.t_edit_file({"path": str(f), "old": "x", "new": "y"}, ctx)
    assert "不唯一" in r and f.read_text() == "x x"  # 未改


# ── 执行类（沙箱）──
def test_run_python(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_python"])
    r = tg.t_run_python({"code": "print(6*7)"}, ctx)
    assert "42" in r and "退出码 0" in r


def test_run_python_blocked_plan(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_python"])
    ctx.plan_mode = True
    assert "计划模式" in tg.t_run_python({"code": "print(1)"}, ctx)


def test_run_command(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_command"])
    r = tg.t_run_command({"command": "echo ivyea-ok"}, ctx)
    assert "ivyea-ok" in r


def test_run_command_rejects_dangerous(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_command"])
    r = tg.t_run_command({"command": "git reset --hard"}, ctx)
    assert "拒绝" in r


def test_run_python_runs_in_workspace(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_python"])
    r = tg.t_run_python({"code": "import os; print(os.getcwd())"}, ctx)
    assert str(tmp_path) in r


# ── web_fetch（monkeypatch，不打真实网络）──
def test_web_fetch_strips_html(tmp_path, monkeypatch):
    class _R:
        status_code = 200
        text = "<html><body><script>x()</script><p>价格 9.9</p></body></html>"
        headers = {"content-type": "text/html"}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _R())
    out = tg.t_web_fetch({"url": "https://example.com"}, _ctx(tmp_path))
    assert "价格 9.9" in out and "<script>" not in out and "x()" not in out


def test_web_fetch_bad_url(tmp_path):
    assert "http" in tg.t_web_fetch({"url": "ftp://x"}, _ctx(tmp_path))


# ── 注册完整性 ──
def test_registered_in_agent_tools():
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    for n in tg.GENERAL_DISPATCH:
        assert n in names and n in _DISPATCH


def test_task_tools_update_bound_task(tmp_path, monkeypatch):
    from ivyea_agent import task_runner

    monkeypatch.setattr(task_runner, "TASK_DIR", tmp_path / "tasks")
    task = task_runner.create("Tool task", steps=["inspect", "finish"])
    ctx = _ctx(tmp_path)
    ctx.task_id = task["id"]

    assert "Tool task" in tg.t_task_read({}, ctx)
    out = tg.t_task_step({"index": 1, "status": "in_progress", "notes": "reading"}, ctx)
    assert "reading" in out
    out = tg.t_task_step({"index": 1, "status": "completed", "notes": "done"}, ctx)
    assert "done" in out
    out = tg.t_task_log({"text": "agent progress"}, ctx)
    assert "agent progress" in out
    assert "继续 Ivyea 长任务" in tg.t_task_resume({}, ctx)

    saved = task_runner.load(task["id"])
    assert saved["steps"][0]["status"] == "completed"
    assert saved["events"][-1]["kind"] == "agent"
