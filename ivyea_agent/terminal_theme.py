"""Shared terminal colour and syntax-highlighting helpers.

Structural UI colours stay intentionally small; source code gets a richer
256-colour palette through Pygments.  Every public helper degrades to readable
plain text when ``NO_COLOR`` is set or Pygments is unavailable.
"""
from __future__ import annotations

import os
import re


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RED = "\033[31m"
WHITE = "\033[37m"

ANSI_RE = re.compile(r"\033\[[0-9;]*m")

try:  # Declared runtime dependency; keep source checkouts resilient as well.
    from pygments import highlight as _pygments_highlight
    from pygments.formatters import Terminal256Formatter
    from pygments.lexers import TextLexer, get_lexer_by_name, get_lexer_for_filename, guess_lexer
    from pygments.style import Style
    from pygments.token import Comment, Generic, Keyword, Name, Number, Operator, Punctuation, String, Text

    class IvyeaTerminalStyle(Style):
        """Dark-terminal palette inspired by the screenshot without backgrounds."""

        background_color = ""
        default_style = ""
        styles = {
            Text: "#abb2bf",
            Comment: "italic #6c7086",
            Keyword: "bold #c678dd",
            Keyword.Type: "#56b6c2",
            Name: "#abb2bf",
            Name.Attribute: "#61afef",
            Name.Builtin: "#61afef",
            Name.Class: "bold #56b6c2",
            Name.Decorator: "#e5c07b",
            Name.Function: "bold #56b6c2",
            Name.Namespace: "#61afef",
            Name.Tag: "#e06c75",
            Name.Variable: "#d19a66",
            String: "#98c379",
            Number: "#e5c07b",
            Operator: "#56b6c2",
            Punctuation: "#abb2bf",
            Generic.Heading: "bold #56b6c2",
            Generic.Subheading: "bold #c678dd",
            Generic.Inserted: "#98c379",
            Generic.Deleted: "#e06c75",
            Generic.Error: "bold #e06c75",
        }

    _PYGMENTS = True
except ImportError:  # pragma: no cover - exercised only in deliberately minimal installs
    _PYGMENTS = False
    IvyeaTerminalStyle = object  # type: ignore[assignment,misc]


_LANG_ALIASES = {
    "py": "python",
    "python3": "python",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "js": "javascript",
    "ts": "typescript",
    "yml": "yaml",
    "md": "markdown",
    "c++": "cpp",
}


def color_enabled(color: bool | None = None) -> bool:
    if "NO_COLOR" in os.environ:
        return False
    return True if color is None else bool(color)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def _looks_like_code(code: str) -> bool:
    return bool(re.search(
        r"(?:^|\n)\s*(?:def |class |import |from |const |let |function |SELECT |#!/|\{\s*$)"
        r"|[;{}]\s*$|\b(?:return|async|await|echo|export)\b",
        code or "",
        re.M | re.I,
    ))


def _lexer(code: str, language: str = "", filename: str = ""):
    if not _PYGMENTS:
        return None
    lang = _LANG_ALIASES.get((language or "").lower().strip(), (language or "").lower().strip())
    if lang:
        try:
            return get_lexer_by_name(lang, stripnl=False, ensurenl=False)
        except Exception:
            pass
    if filename:
        try:
            return get_lexer_for_filename(filename, code, stripnl=False, ensurenl=False)
        except Exception:
            pass
    if not lang and _looks_like_code(code):
        try:
            return guess_lexer(code, stripnl=False, ensurenl=False)
        except Exception:
            pass
    return TextLexer(stripnl=False, ensurenl=False)


def highlight_code(code: str, *, language: str = "", filename: str = "",
                   color: bool | None = None) -> str:
    """Highlight a complete source snippet while preserving text and line count."""
    text = str(code or "")
    if not text or not color_enabled(color):
        return text
    lexer = _lexer(text, language=language, filename=filename)
    if lexer is None:
        return f"{WHITE}{DIM}{text}{RESET}"
    try:
        rendered = _pygments_highlight(
            text,
            lexer,
            Terminal256Formatter(style=IvyeaTerminalStyle),
        )
        return rendered if text.endswith("\n") else rendered.rstrip("\n")
    except Exception:
        return f"{WHITE}{DIM}{text}{RESET}"


def highlight_line(code: str, *, language: str = "", filename: str = "",
                   color: bool | None = None) -> str:
    """Single-line convenience used by diff renderers."""
    return highlight_code(code, language=language, filename=filename, color=color).rstrip("\r\n")
