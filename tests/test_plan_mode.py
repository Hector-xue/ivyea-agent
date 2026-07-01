"""自然语言进/出计划模式 + todo_write 纪律守卫。"""
from __future__ import annotations

import pytest

from ivyea_agent.cli import _plan_mode_intent
from ivyea_agent.tools_general import t_todo_write


@pytest.mark.parametrize("line,expect", [
    ("进入计划模式", "enter"),
    ("打开计划模式", "enter"),
    ("开计划模式", "enter"),
    ("计划模式", "enter"),
    ("plan mode", "enter"),
    ("进入计划模式。", "enter"),       # 句末标点不影响
    ("  退出计划模式  ", "exit"),      # 首尾空白不影响
    ("关闭计划模式", "exit"),
    ("退出计划", "exit"),
    # 长句/嵌入短语不能误吞成命令
    ("帮我分析进入计划模式怎么实现", None),
    ("我想进入计划模式看看效果", None),
    ("读取计划模式相关代码", None),
    ("你好", None),
    ("", None),
])
def test_plan_mode_intent(line, expect):
    assert _plan_mode_intent(line) == expect


class _Ctx:
    todos: list = []


def test_todo_write_warns_multiple_in_progress():
    out = t_todo_write({"todos": [
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
    ]}, _Ctx())
    assert "2 个 in_progress" in out


def test_todo_write_nudges_no_in_progress():
    out = t_todo_write({"todos": [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "pending"},
    ]}, _Ctx())
    assert "下一步" in out


def test_todo_write_clean_single_in_progress():
    out = t_todo_write({"todos": [
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "pending"},
    ]}, _Ctx())
    assert out == "已更新计划：0/2 完成。"
