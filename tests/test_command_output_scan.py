"""命令写出来的文件也要被记成产物。

存在的理由：`file_change` 原本只有 write_file / edit_file 会发。真实任务里报表和
图表大多是 run_python / run_command 写出来的 —— 那些产物一条都没被记上，界面上
「文件」那格永远是空的，功能等于不存在（生产库里 0 行就是这么来的）。
"""
from __future__ import annotations

import time


from ivyea_agent import tools_general
from ivyea_agent.agent_tools import ToolContext


def _Ctx(workspace):
    c = ToolContext(workspace=str(workspace), session_id="s1", turn_id="t1")
    c.perm.prompt_fn = lambda *a, **k: "approve"      # 审批门不在本用例的射程内
    return c


def _run_cmd(ctx, command: str) -> str:
    return tools_general.t_run_command({"command": command}, ctx)


def test_file_written_by_command_is_recorded(tmp_path):
    ctx = _Ctx(tmp_path)
    _run_cmd(ctx, "echo hello > report.md")
    paths = [c["path"] for c in ctx.file_changes]
    assert any(p.endswith("report.md") for p in paths)
    entry = next(c for c in ctx.file_changes if c["path"].endswith("report.md"))
    # 拿不到改之前的内容，就别猜是新建还是覆盖 —— 照实说"写出"
    assert entry["action"] == "write"
    assert entry["diff"] == ""


def test_untouched_files_are_not_recorded(tmp_path):
    import os

    old = tmp_path / "old.txt"
    old.write_text("x", encoding="utf-8")
    # 把旧文件的 mtime 推到很久以前，模拟"上一轮留下的文件"
    long_ago = time.time() - 3600
    os.utime(old, (long_ago, long_ago))

    ctx = _Ctx(tmp_path)
    # `echo x > f` 在 bash 和 cmd 下行为一致，这条可以真走 shell
    _run_cmd(ctx, "echo new > fresh.txt")
    paths = [c["path"] for c in ctx.file_changes]
    assert any(p.endswith("fresh.txt") for p in paths)
    assert str(old) not in paths, "没动过的文件不该被记成这一轮的产物"


# 下面两条直接调 _scan_command_outputs，不经过 shell。
# **这本身就是这次要修的那个毛病**：第一版用 `mkdir -p`/`touch`/`for … in $(seq)`
# 造现场，在 Windows 的 `cmd /c` 下这些命令根本不存在，整条链失败、文件没生成，
# 断言随之落空 —— CI 的 windows-latest 三个 Python 版本全挂。给"别假设是 Linux"
# 写的测试自己假设了 Linux。造现场用 pathlib，跨平台才是真的一致。

def test_noise_directories_are_skipped(tmp_path):
    """一次 npm install 能写几万个文件，全记下来等于把事件流灌爆。"""
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "abc").write_text("x", encoding="utf-8")
    (tmp_path / "result.csv").write_text("ok", encoding="utf-8")

    ctx = _Ctx(tmp_path)
    tools_general._scan_command_outputs(ctx, str(tmp_path), time.time() - 60)
    paths = [c["path"] for c in ctx.file_changes]
    assert any(p.endswith("result.csv") for p in paths)
    assert not any("node_modules" in p for p in paths)
    assert not any("objects" in p for p in paths)


def test_recording_is_capped(tmp_path):
    for i in range(tools_general._MAX_FILE_CHANGES + 20):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    ctx = _Ctx(tmp_path)
    tools_general._scan_command_outputs(ctx, str(tmp_path), time.time() - 60)
    assert len(ctx.file_changes) == tools_general._MAX_FILE_CHANGES


def test_python_output_is_recorded(tmp_path):
    ctx = _Ctx(tmp_path)
    tools_general.t_run_python(
        {"code": "open('out.json','w').write('{}')"}, ctx)
    assert any(c["path"].endswith("out.json") for c in ctx.file_changes)


def test_scan_never_breaks_the_command_itself(tmp_path, monkeypatch):
    """扫描失败绝不能影响命令的返回 —— 产物索引是附加值，不是主线。"""
    ctx = _Ctx(tmp_path)

    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(tools_general.os, "walk", boom)
    out = _run_cmd(ctx, "echo hello")
    assert "hello" in out


def test_no_workspace_is_harmless(tmp_path):
    ctx = _Ctx(tmp_path / "does-not-exist")
    tools_general._scan_command_outputs(ctx, str(tmp_path / "nope"), time.time())
    assert ctx.file_changes == []
