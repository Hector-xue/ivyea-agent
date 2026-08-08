"""文件变更事件 —— 产物栏那格 diff 的数据源。

step 事件里虽然有 `path`，但它只说明"调用了 write_file"，说不出**改了什么**。
这些用例钉住三件事：diff 与磁盘真实结果一致、**没落盘的改动绝不出现**、
事件体积有上限。
"""
from __future__ import annotations

import pathlib

import pytest

from ivyea_agent import stream_json, tools_general
from ivyea_agent.agent_tools import ToolContext


@pytest.fixture
def ctx(tmp_path):
    c = ToolContext(workspace=str(tmp_path), session_id="s1", turn_id="t1")
    c.perm.prompt_fn = lambda *a, **k: "approve"      # 审批门不在本用例的射程内
    return c


def test_new_file_is_recorded_as_a_create(ctx, tmp_path):
    tools_general.t_write_file({"path": "a.txt", "content": "一\n二\n"}, ctx)
    ch = ctx.file_changes[0]
    assert ch["action"] == "create" and ch["scope"] == "file"
    assert "+ 一" in ch["diff"] and "+ 二" in ch["diff"]


def test_overwrite_diff_matches_what_landed_on_disk(ctx, tmp_path):
    """diff 说改成什么样，磁盘上就得是什么样 —— 对不上比不显示更糟。"""
    tools_general.t_write_file({"path": "a.txt", "content": "一\n二\n"}, ctx)
    tools_general.t_write_file({"path": "a.txt", "content": "一\n改过的二\n"}, ctx)
    ch = ctx.file_changes[-1]
    assert ch["action"] == "overwrite"
    assert "- 二" in ch["diff"] and "+ 改过的二" in ch["diff"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "一\n改过的二\n"


def test_edit_is_a_fragment_not_the_whole_file(ctx, tmp_path):
    """edit_file 的 diff 只是被替换的那一段，行号是**片段内**的相对行号。
    标成 file 的话，界面会拿它去对文件行号，对不上。"""
    tools_general.t_write_file({"path": "a.txt", "content": "一\n二\n三\n"}, ctx)
    tools_general.t_read_file({"path": "a.txt"}, ctx)        # 满足"改前必读"护栏
    tools_general.t_edit_file({"path": "a.txt", "old": "二", "new": "贰"}, ctx)
    ch = ctx.file_changes[-1]
    assert ch["action"] == "edit" and ch["scope"] == "fragment"
    assert "- 二" in ch["diff"] and "+ 贰" in ch["diff"]
    assert "三" not in ch["diff"]                            # 没被改的行不该混进来
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "一\n贰\n三\n"


def test_a_denied_write_leaves_no_trace(tmp_path):
    """**最要紧的一条**：被审批拒掉的改动绝不能出现在界面的 diff 里 ——
    用户会以为改已经落地了。"""
    c = ToolContext(workspace=str(tmp_path), session_id="s1", turn_id="t1")
    c.perm.prompt_fn = lambda *a, **k: "deny"
    tools_general.t_write_file({"path": "b.txt", "content": "x"}, c)
    assert c.file_changes == []
    assert not (tmp_path / "b.txt").exists()


def test_a_failed_write_leaves_no_trace(ctx, tmp_path, monkeypatch):
    """写盘本身失败（磁盘满、权限）同样不该留记录。"""
    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    out = tools_general.t_write_file({"path": "c.txt", "content": "x"}, ctx)
    assert "失败" in out
    assert ctx.file_changes == []


def test_changes_per_turn_are_capped(ctx):
    """写循环跑飞时不能把事件流灌爆。"""
    for i in range(60):
        tools_general.t_write_file({"path": f"f{i}.txt", "content": "x"}, ctx)
    assert len(ctx.file_changes) == tools_general._MAX_FILE_CHANGES


def test_event_truncates_giant_diffs():
    """一个几万行的文件，整段 diff 塞进 SSE 会把一轮撑到几 MB。"""
    ev = stream_json.file_change_event("s", "t", "/tmp/a", "overwrite", "x" * 20000)
    assert ev["truncated"] is True and len(ev["diff"]) == stream_json._DIFF_MAX
    small = stream_json.file_change_event("s", "t", "/tmp/a", "edit", "短", "fragment")
    assert small["truncated"] is False and small["scope"] == "fragment"


def test_diff_carries_no_ansi_escapes():
    """这份 diff 是给网页看的。带 ANSI 转义就是一串乱码。"""
    ctx = ToolContext(session_id="s", turn_id="t")
    assert getattr(ctx, "file_changes") == []
    from ivyea_agent import panels
    d = panels.render_diff("一\n", "二\n", "a.txt", color=False)
    assert "\x1b[" not in d


def test_serve_forwards_file_change_to_the_browser():
    """serve 的结构化事件通道是**白名单**制的。

    漏掉一个类型不会报任何错 —— 事件被静默丢掉，界面上那一格永远是空的，
    而后端测试全绿。所以把放行名单钉在这里。
    """
    import inspect

    from ivyea_agent import service

    src = inspect.getsource(service.chat_stream)
    assert '"file_change"' in src, "file_change 不在 serve 的事件放行名单里，前端永远收不到"
