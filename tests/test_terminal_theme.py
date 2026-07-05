from __future__ import annotations

from ivyea_agent import terminal_theme


def test_python_highlight_preserves_text_and_uses_semantic_colors(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    code = 'def hello(name: str) -> str:\n    return f"hi {name}"  # greet'
    out = terminal_theme.highlight_code(code, language="python")
    assert terminal_theme.strip_ansi(out) == code
    assert "\033[38;5;" in out
    assert "def" in out and "hello" in out and "# greet" in out
    assert out.count("\n") == code.count("\n")


def test_multiple_languages_and_filename_detection(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    samples = [
        ('{"enabled": true, "count": 3}', "json", ""),
        ('export MODEL="gpt-5.5"\necho "$MODEL"', "bash", ""),
        ("const answer: number = 42", "typescript", ""),
        ("SELECT asin FROM products WHERE active = 1;", "", "query.sql"),
    ]
    for code, language, filename in samples:
        out = terminal_theme.highlight_code(code, language=language, filename=filename)
        assert terminal_theme.strip_ansi(out) == code
        assert "\033[" in out


def test_no_color_is_exact_plain_text(monkeypatch):
    code = "def answer():\n\n    return 42"
    monkeypatch.setenv("NO_COLOR", "1")
    assert terminal_theme.highlight_code(code, language="python") == code
    assert terminal_theme.highlight_line("return 42", language="python") == "return 42"
    monkeypatch.setenv("NO_COLOR", "")               # NO_COLOR 规范：只要变量存在就禁色
    assert terminal_theme.highlight_code(code, language="python") == code


def test_highlighter_never_adds_background_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = terminal_theme.highlight_code("return 42", language="python")
    assert "48;5" not in out
