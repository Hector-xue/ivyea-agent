"""Chat UI rendering helpers."""
from __future__ import annotations

import io

from ivyea_agent import chat_ui


def test_live_spinner_busy_composer_renders_status_above_prompt(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(chat_ui.sys, "stdout", buf)
    monkeypatch.setattr(chat_ui.shutil, "get_terminal_size", lambda fallback: chat_ui.shutil.os.terminal_size((80, 24)))

    spin = chat_ui._LiveSpinner(busy_composer=True)
    spin.tick("hello world")
    out = buf.getvalue()

    assert "生成中" in out
    assert "任务运行中" in out
    assert "Ctrl-C/Esc 中断" in out
    assert "╰─ ❯" in out

    spin.set_input_preview("revise task")
    spin.tick()
    assert "revise task" in buf.getvalue()

    spin.clear()
    assert "\033[2A" in buf.getvalue()


def test_live_spinner_legacy_single_line_mode(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(chat_ui.sys, "stdout", buf)
    monkeypatch.setattr(chat_ui.shutil, "get_terminal_size", lambda fallback: chat_ui.shutil.os.terminal_size((80, 24)))

    spin = chat_ui._LiveSpinner(busy_composer=False)
    spin.tick("hello world")
    out = buf.getvalue()

    assert "生成中" in out
    assert "任务运行中" not in out
    spin.clear()
    assert buf.getvalue().endswith("\r\033[K")


def test_busy_input_non_tty_is_noop(monkeypatch):
    class _FakeStdin:
        def isatty(self):
            return False

    monkeypatch.setattr(chat_ui.sys, "stdin", _FakeStdin())
    busy = chat_ui.BusyInput(enabled=True)

    assert busy.enabled is False
    assert busy.cancel_check() is False
    assert busy.pop_queued() == ""


def test_busy_input_poll_queues_text_and_cancel(monkeypatch):
    class _FakeStdin:
        def __init__(self, chars):
            self.chars = list(chars)
        def isatty(self):
            return True
        def read(self, _n):
            return self.chars.pop(0) if self.chars else ""

    fake = _FakeStdin("next task\n")
    previews = []
    monkeypatch.setattr(chat_ui.sys, "stdin", fake)
    monkeypatch.setattr(chat_ui.select, "select", lambda r, w, x, timeout=0: ([fake] if fake.chars else [], [], []), raising=False)
    busy = chat_ui.BusyInput(enabled=True, on_change=previews.append)
    busy.poll()

    assert busy.pop_queued() == "next task"
    assert busy.cancel_check() is False
    assert "next task" in previews
    assert previews[-1] == "已排队：next task"

    fake2 = _FakeStdin("\x1b")
    monkeypatch.setattr(chat_ui.sys, "stdin", fake2)
    monkeypatch.setattr(chat_ui.select, "select", lambda r, w, x, timeout=0: ([fake2] if fake2.chars else [], [], []), raising=False)
    busy2 = chat_ui.BusyInput(enabled=True)
    busy2.poll()
    assert busy2.cancelled is True
