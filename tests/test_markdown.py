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


def test_code_block_has_multilanguage_syntax_colors_and_preserves_blank_lines(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    md = '''```python
def hello(name):
    # blank line below

    return f"hi {name}"
```

```json
{"ok": true, "count": 2}
```'''
    out = markdown.render(md)
    plain = _plain(out)
    assert "def hello" in plain and "# blank line below" in plain
    assert '{"ok": true, "count": 2}' in plain
    assert "\033[38;5;" in out
    assert "│\n" in plain                         # 代码块内部空行仍有边框，不被截断
    assert plain.count("╭─") == 2 and plain.count("╰─") == 2


def test_tilde_fence_and_inline_code_get_structural_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = markdown.render("运行 `ivyea chat`：\n\n~~~bash\necho ok\n~~~")
    assert "`" not in out and "~~~" not in out
    assert "\033[36mivyea chat\033[0m" in out
    assert "bash" in _plain(out) and "echo ok" in _plain(out)


def test_stream_block_splitter_never_splits_inside_fence():
    md = "前言\n\n```python\ndef hello():\n\n    return 1\n```\n\n结尾"
    blocks, remainder = markdown.split_stream_blocks(md)
    assert blocks == ["前言", "```python\ndef hello():\n\n    return 1\n```"]
    assert remainder == "结尾"


def test_stream_block_splitter_waits_for_closing_fence():
    blocks, remainder = markdown.split_stream_blocks("```python\ndef hello():\n\n")
    assert blocks == []
    assert remainder.startswith("```python")


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
