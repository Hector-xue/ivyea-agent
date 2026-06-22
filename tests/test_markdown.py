"""markdown → ANSI 渲染。"""
from __future__ import annotations

import re

from ivyea_agent import markdown


def _plain(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)


def test_heading_and_bold(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = markdown.render("# 标题\n这是 **重点** 内容")
    assert "标题" in out and "\033[1m" in out          # 有粗体 ANSI
    assert "**" not in out                              # markdown 语法不外泄
    assert "#" not in _plain(out).split("\n")[0]        # 一级标题不留 #


def test_inline_code_and_list():
    out = markdown.render("- 跑 `ivyea patrol`\n- 看报告")
    assert "•" in out                                   # 列表项变圆点
    assert "`" not in out
    assert "ivyea patrol" in _plain(out)
    assert "48;5" not in out and "bg:" not in out


def test_code_block():
    out = markdown.render("```bash\nivyea lingxing probe\n```")
    assert "ivyea lingxing probe" in _plain(out)
    assert "```" not in out
    assert "48;5" not in out


def test_table():
    md = "| 杠杆 | 数量 |\n| --- | --- |\n| 否词 | 3 |"
    out = markdown.render(md)
    plain = _plain(out)
    assert "杠杆" in plain and "否词" in plain
    assert "│" in out                                   # 渲染成竖线表格
    assert "---" not in plain                           # 分隔行被吃掉


def test_ordered_list_and_rule():
    out = markdown.render("1. 第一\n2. 第二\n\n---")
    plain = _plain(out)
    assert "1." in plain and "─" in out
