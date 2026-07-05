from __future__ import annotations

from ivyea_agent import chat_ui


def test_line_streamer_keeps_fenced_code_with_internal_blank_together(monkeypatch):
    emitted = []
    printer = chat_ui._StreamPrinter()
    monkeypatch.setattr(printer, "_erase", lambda _text: None)
    monkeypatch.setattr(printer, "_emit_md", emitted.append)
    monkeypatch.setattr(chat_ui.sys.stdout, "write", lambda _text: None)
    monkeypatch.setattr(chat_ui.sys.stdout, "flush", lambda: None)

    printer.render("```python\ndef hello():\n\n")
    assert emitted == []
    printer.render("    return 1\n```\n\n下一段")
    assert emitted == ["```python\ndef hello():\n\n    return 1\n```"]
    assert printer.block == "下一段"
