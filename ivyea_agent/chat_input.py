"""Claude Code 风格的输入框（prompt_toolkit）。

默认使用轻量 PromptSession + ❯ 提示 + 斜杠补全 + ↑↓历史 + 粘贴(bracketed paste)。
设置 IVYEA_BOXED_INPUT=1 才启用带边框输入区（Frame）。
三级降级：PromptSession/可选带框 Application → 内置 input()，保证任何环境可用。
"""
from __future__ import annotations

import sys
import os
from typing import Callable

from . import config

EXIT = object()  # 哨兵：用户在框内 Ctrl+C/Ctrl+D 退出


class ChatInput:
    def __init__(self, slash_commands: list, status_fn: Callable[[], str]):
        self.slash = slash_commands
        self.status_fn = status_fn
        self._app_factory = None      # 带框 Application（每次新建）
        self._session = None          # 普通 PromptSession 兜底
        self._mode = "plain"
        if sys.stdin.isatty():
            self._try_setup()

    def _completer(self):
        from prompt_toolkit.completion import Completer, Completion

        class _C(Completer):
            def __init__(s, cmds): s.cmds = cmds
            def get_completions(s, document, complete_event):
                t = document.text_before_cursor
                if not t.startswith("/"):
                    return
                for cmd, desc in s.cmds:
                    if cmd.startswith(t):
                        yield Completion(cmd, start_position=-len(t), display=cmd, display_meta=desc)
        return _C(self.slash)

    def _try_setup(self):
        try:
            from prompt_toolkit.history import FileHistory
            config.ensure_dirs()
            self._history = FileHistory(str(config.IVYEA_DIR / "chat_history"))
            if self._boxed_enabled():
                from prompt_toolkit.widgets import Frame, TextArea  # noqa: F401
                from prompt_toolkit.application import Application   # noqa: F401
                self._mode = "boxed"
                return
            self._setup_session()
        except Exception:
            self._mode = "plain"

    @staticmethod
    def _boxed_enabled() -> bool:
        return os.environ.get("IVYEA_BOXED_INPUT", "").strip() == "1"

    def _setup_session(self) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.styles import Style
        from prompt_toolkit.shortcuts.prompt import CompleteStyle
        self._session = PromptSession(
            history=self._history,
            completer=self._completer(), complete_while_typing=True,
            complete_style=CompleteStyle.READLINE_LIKE,
            style=Style.from_dict(self._style_dict()))
        self._mode = "session"

    @staticmethod
    def _style_dict() -> dict[str, str]:
        return {
            "frame.border": "ansicyan",
            "hint": "ansibrightblack",
            "prompt": "ansicyan bold",
            "completion-menu": "#d1d5db",
            "completion-menu.completion": "#d1d5db",
            "completion-menu.completion.current": "ansicyan bold",
            "completion-menu.meta.completion": "ansibrightblack",
            "scrollbar.background": "ansibrightblack",
            "scrollbar.button": "ansicyan",
        }

    def _read_boxed(self) -> object:
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.widgets import Frame, TextArea
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        ta = TextArea(prompt=[("class:prompt", "❯ ")], multiline=False, wrap_lines=True,
                      completer=self._completer(), complete_while_typing=True,
                      history=self._history)
        frame = Frame(ta)
        hint = Window(FormattedTextControl(lambda: self.status_fn()), height=1, style="class:hint")
        root = HSplit([frame, hint])
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            event.app.exit(result=ta.text)

        @kb.add("c-c")
        @kb.add("c-d")
        def _(event):
            event.app.exit(result=EXIT)

        style = Style.from_dict(self._style_dict())
        app = Application(layout=Layout(root), key_bindings=kb,
                          style=style, full_screen=False, mouse_support=False)
        return app.run()

    def read(self, plain_prompt: str = "❯ ") -> object:
        """返回输入字符串；用户退出返回 EXIT 哨兵。"""
        if self._mode == "boxed":
            try:
                r = self._read_boxed()
                return r if r is EXIT else (r or "").strip()
            except (EOFError, KeyboardInterrupt):
                return EXIT
            except Exception:
                self._mode = "session" if self._session else "plain"  # 出错降级
        if self._mode == "session" and self._session is not None:
            try:
                from prompt_toolkit.formatted_text import ANSI
                return self._session.prompt(ANSI(plain_prompt)).strip()
            except (EOFError, KeyboardInterrupt):
                return EXIT
        try:
            return input(plain_prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return EXIT
