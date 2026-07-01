"""全屏 TUI 聊天骨架（P0）。"""
from __future__ import annotations

import io
import re

import pytest

from ivyea_agent import chat_tui


def _plain(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)


def test_tui_disabled_when_not_tty(monkeypatch):
    # 测试环境非 TTY：无论 IVYEA_TUI 如何都应回退（返回 False）
    monkeypatch.setenv("IVYEA_TUI", "1")
    assert chat_tui.tui_enabled() is False


def _run_headless(keys: str, status="  ivyea · GPT-5.5 · dry-run "):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.data_structures import Size
    import prompt_toolkit.application as ptkapp

    buf = io.StringIO()
    with create_pipe_input() as pinp:
        orig = ptkapp.Application

        def factory(*a, **k):
            k.setdefault("input", pinp)
            k.setdefault("output", Vt100_Output(buf, lambda: Size(rows=30, columns=100), term="xterm-256color"))
            return orig(*a, **k)

        ptkapp.Application = factory
        try:
            pinp.send_text(keys)
            rc = chat_tui.run(lambda: status, [("/model", "x"), ("/exit", "退出")])
        finally:
            ptkapp.Application = orig
    return rc, _plain(buf.getvalue())


def test_skeleton_sticky_header_and_clean_exit():
    rc, vis = _run_headless("排查任务\r/exit\r")
    assert rc == 0                                   # /exit 干净退出
    assert "当前指令：排查任务" in vis               # sticky 头部显示最近指令
    assert "GPT-5.5" in vis                          # footer 显示 status
    assert "❯ 排查任务" in vis                       # transcript 回显


def test_skeleton_ctrl_c_exits():
    rc, _ = _run_headless("\x03")                    # Ctrl-C 退出
    assert rc == 0


# ---- P1 核心闭环 ----
def _wait_idle(tui, timeout=2.0):
    import time
    end = time.time() + timeout
    while time.time() < end and tui.running:
        time.sleep(0.02)


def test_turn_streams_and_interleaves_tool_lines():
    import time
    def fake_turn(line, render, narrate):
        render("你好"); narrate("⏺ 读取文件"); render("，世界")
        return {"text": "你好，世界", "usage": {}, "blocked": False}

    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=fake_turn, render_markdown=lambda s: s)
    tui._start_turn("改 greet.py")
    _wait_idle(tui)
    assert tui.running is False
    assert tui.instruction == "改 greet.py"          # sticky 头部指令
    assert tui.live is None                           # 流式已定稿
    plain = _plain("\n".join(tui.blocks))
    assert "❯ 改 greet.py" in plain                   # 用户回显
    assert "你好" in plain and "，世界" in plain      # 两段流式文本
    # Claude 式交错：文本 → 工具行 → 继续文本
    assert plain.index("你好") < plain.index("⏺ 读取文件") < plain.index("，世界")


def test_turn_error_surfaced():
    def boom(line, render, narrate):
        raise RuntimeError("炸了")
    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=boom, render_markdown=lambda s: s)
    tui._start_turn("x")
    _wait_idle(tui)
    assert tui.running is False
    assert "炸了" in _plain("\n".join(tui.blocks))
