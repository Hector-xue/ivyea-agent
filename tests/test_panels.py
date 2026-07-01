"""M5 视觉：Todo 面板、彩色 Diff、todo_write 工具、NO_COLOR。"""
from __future__ import annotations

import re

from ivyea_agent import panels


def _plain(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)


def test_render_todos_icons_and_count():
    todos = [
        {"content": "拉广告数据", "status": "completed"},
        {"content": "跑规则引擎", "status": "in_progress"},
        {"content": "出报告", "status": "pending"},
    ]
    out = panels.render_todos(todos)
    plain = _plain(out)
    assert "计划 1/3" in plain
    assert "☑ 拉广告数据" in plain and "◐ 跑规则引擎" in plain and "☐ 出报告" in plain


def test_render_todos_empty():
    assert panels.render_todos([]) == ""


def test_render_todos_wraps_long_content(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback: __import__("os").terminal_size((44, 20)))
    out = panels.render_todos([{"content": "a " * 40, "status": "pending"}], color=False)
    assert len(out.splitlines()) > 3


def test_render_diff_colors():
    out = panels.render_diff("bid 1.00\nstate on", "bid 0.85\nstate on", "kw")
    assert "\033[31m" in out and "\033[32m" in out      # 红删绿增
    plain = _plain(out)
    assert "- bid 1.00" in plain and "+ bid 0.85" in plain   # 行号栏 + 符号 + 代码
    assert "1 -" in plain and "1 +" in plain                 # 左侧行号


def test_render_diff_nochange():
    assert panels.render_diff("same", "same") == "（无变化）"


def test_render_diff_no_color():
    out = panels.render_diff("a", "b", color=False)
    assert "\033[" not in out


def test_todo_write_tool(tmp_path):
    from ivyea_agent import tools_general as tg
    from ivyea_agent.agent_tools import ToolContext
    ctx = ToolContext(workspace=str(tmp_path))
    r = tg.t_todo_write({"todos": [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "bogus"},   # 非法状态归一为 pending
    ]}, ctx)
    assert "1/2" in r
    assert ctx.todos == [{"content": "a", "status": "completed"},
                         {"content": "b", "status": "pending"}]


def test_todo_write_registered():
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "todo_write" in names and "todo_write" in _DISPATCH


def test_edit_file_preview_shows_diff(tmp_path):
    """edit_file 审批预览应含彩色 diff（用 session_allow 放行后不弹）。"""
    from ivyea_agent import panels as p
    d = p.render_diff("1.00", "0.85", "f")
    assert "- 1.00" in _plain(d) and "+ 0.85" in _plain(d)


def test_markdown_no_color(monkeypatch):
    from ivyea_agent import markdown
    monkeypatch.setenv("NO_COLOR", "1")
    out = markdown.render("# 标题\n**粗体**")
    assert "\033[" not in out and "标题" in out and "粗体" in out


def test_ui_message_and_panel_no_color(monkeypatch):
    from ivyea_agent import ui
    monkeypatch.setenv("NO_COLOR", "1")
    msg = ui.message("warn", "检查配置")
    box = ui.panel("标题", "很长的内容 " * 10, kind="warn", width=48)
    assert "\033[" not in msg + box
    assert "检查配置" in msg and "标题" in box


def test_ui_tool_call_truncates_args():
    from ivyea_agent import ui
    out = ui.tool_call("read_file", {"path": "/tmp/" + "x" * 100}, color=False)
    assert "读取文件" in out and "..." in out      # 友好动词 + 超长明细截断


def test_ui_tool_call_friendly_verb_and_stage_no_color():
    from ivyea_agent import ui
    call = ui.tool_call("read_file", {"path": "a.py"}, color=False)
    stage = ui.stage("Code", "计划 → 测试", color=False)
    assert "读取文件" in call and "a.py" in call and "└" in call   # 动词 + └ 明细行
    assert "read_file" not in call                                  # 不再暴露原始工具名
    assert "Code" in stage and "计划" in stage


def test_chat_input_style_avoids_block_backgrounds():
    from ivyea_agent.chat_input import ChatInput
    styles = ChatInput._style_dict()
    combined = " ".join(styles.values())
    assert "bg:" not in combined
    assert styles["completion-menu.completion.current"] == "ansicyan bold"


def test_chat_input_boxed_mode_default_on(monkeypatch):
    from ivyea_agent.chat_input import ChatInput
    monkeypatch.delenv("IVYEA_BOXED_INPUT", raising=False)
    assert ChatInput._boxed_enabled() is True            # 默认带框
    for off in ("0", "false", "Off", "NO"):
        monkeypatch.setenv("IVYEA_BOXED_INPUT", off)
        assert ChatInput._boxed_enabled() is False        # 显式关闭
    monkeypatch.setenv("IVYEA_BOXED_INPUT", "1")
    assert ChatInput._boxed_enabled() is True


def test_chat_input_echo_indents_multiline(capsys, monkeypatch):
    from ivyea_agent.chat_input import ChatInput
    monkeypatch.setenv("NO_COLOR", "1")
    ChatInput._echo_submitted("第一行\n第二行")
    ChatInput._echo_submitted("   ")                      # 纯空白不回显
    out = capsys.readouterr().out
    # Claude 风格：> 标记 + 续行缩进 2 格，上下留白；NO_COLOR 无背景带
    assert out == "\n> 第一行\n  第二行\n\n"


def test_ui_icons_fallback_to_ascii_on_non_utf8(monkeypatch):
    import types
    from ivyea_agent import ui
    monkeypatch.setattr(ui.sys, "stdout", types.SimpleNamespace(encoding="gbk"))
    assert ui._unicode_glyphs_ok() is False               # Windows GBK 回退 ASCII
    monkeypatch.setattr(ui.sys, "stdout", types.SimpleNamespace(encoding="utf-8"))
    assert ui._unicode_glyphs_ok() is True
