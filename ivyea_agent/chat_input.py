"""Claude Code 风格的输入框（prompt_toolkit）。

默认使用带边框输入区（Frame）：输入框始终钉在底部，提交后把输入回显成一行
静态历史，对话自上而下在框上方累积——观感与 Claude Code / Codex / Hermes 一致。
带框模式 + ❯ 提示 + 斜杠补全 + ↑↓历史 + 粘贴(bracketed paste)。
设置 IVYEA_BOXED_INPUT=0 可退回轻量 PromptSession 单行输入。
三级降级：带框 Application → PromptSession → 内置 input()，保证任何环境可用。
"""
from __future__ import annotations

import sys
import os
from typing import Callable

from . import config

EXIT = object()  # 哨兵：用户在框内 Ctrl+C/Ctrl+D 退出


class ChatInput:
    def __init__(self, slash_commands: list, status_fn: Callable[[], str],
                 mode_cycle_fn: Callable[[], str] | None = None):
        self.slash = slash_commands
        self.status_fn = status_fn
        self.mode_cycle_fn = mode_cycle_fn   # shift+tab 循环模式：普通→自动接受→计划；返回新模式名
        self._app_factory = None      # 带框 Application（每次新建）
        self._session = None          # 普通 PromptSession 兜底
        self._mode = "plain"
        if sys.stdin.isatty():
            self._try_setup()

    def _completer(self):
        from prompt_toolkit.completion import Completer, Completion
        slash = self.slash

        class _C(Completer):
            def get_completions(s, document, complete_event):
                t = document.text_before_cursor
                if not t.startswith("/"):
                    return
                seen = set()
                for cmd, desc in slash:
                    if cmd.startswith(t):
                        seen.add(cmd)
                        yield Completion(cmd, start_position=-len(t), display=cmd, display_meta=desc)
                try:   # 用户自定义命令 ~/.ivyea/commands/*.md
                    from . import commands as _cmds
                    for name, summary in _cmds.list_commands().items():
                        cmd = "/" + name
                        if cmd.startswith(t) and cmd not in seen:
                            yield Completion(cmd, start_position=-len(t), display=cmd,
                                             display_meta=summary or "自定义命令")
                except Exception:
                    pass
        return _C()

    def _try_setup(self):
        try:
            from prompt_toolkit.history import FileHistory
            config.ensure_dirs()
            self._history = FileHistory(str(config.IVYEA_DIR / "chat_history"))
            self._setup_session()          # 始终建好 PromptSession，作为带框模式的兜底
            if self._boxed_enabled():
                from prompt_toolkit.widgets import Frame, TextArea  # noqa: F401 探测可用性
                from prompt_toolkit.application import Application   # noqa: F401
                self._mode = "boxed"
        except Exception:
            self._mode = "plain"

    @staticmethod
    def _boxed_enabled() -> bool:
        # 默认开启带框输入；IVYEA_BOXED_INPUT=0/false/off/no 可退回轻量单行
        return os.environ.get("IVYEA_BOXED_INPUT", "").strip().lower() not in ("0", "false", "off", "no")

    def _setup_session(self) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.styles import Style
        from prompt_toolkit.shortcuts.prompt import CompleteStyle
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        self._session = PromptSession(
            history=self._history,
            completer=self._completer(), complete_while_typing=True,
            auto_suggest=AutoSuggestFromHistory(),        # 历史 ghost 建议（→/Ctrl-E 接受）
            complete_style=CompleteStyle.MULTI_COLUMN,   # 输入 / 即弹下拉菜单（带描述）
            bottom_toolbar=lambda: self.status_fn(),     # 常驻底部状态栏
            style=Style.from_dict(self._style_dict()))
        self._mode = "session"

    @staticmethod
    def _style_dict() -> dict[str, str]:
        return {
            "frame.border": "ansicyan",
            "hint": "ansibrightblack",
            "prompt": "ansicyan bold",
            "auto-suggestion": "ansibrightblack",        # 历史建议的灰字 ghost text
            "completion-menu": "#d1d5db",
            "completion-menu.completion": "#d1d5db",
            "completion-menu.completion.current": "ansicyan bold",
            "completion-menu.meta.completion": "ansibrightblack",
            "completion-menu.meta.completion.current": "ansicyan",
            "scrollbar.background": "ansibrightblack",
            "scrollbar.button": "ansicyan",
            "bottom-toolbar": "noreverse ansibrightblack",
            "bottom-toolbar.text": "ansibrightblack",
        }

    @staticmethod
    def _rounded_frame(body):
        """圆角边框（╭╮╰╯），与欢迎框/Claude·Codex 风格统一；ptk 自带 Frame 是方角。"""
        from prompt_toolkit.layout.containers import HSplit, VSplit, Window

        def fill(char, width=None, height=None):
            return Window(char=char, style="class:frame.border", width=width, height=height)

        top = VSplit([fill("╭", 1, 1), fill("─", height=1), fill("╮", 1, 1)], height=1)
        mid = VSplit([fill("│", 1), body, fill("│", 1)])
        bot = VSplit([fill("╰", 1, 1), fill("─", height=1), fill("╯", 1, 1)], height=1)
        return HSplit([top, mid, bot])

    def _read_boxed(self) -> object:
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.widgets import TextArea
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

        ta = TextArea(prompt=[("class:prompt", "❯ ")], multiline=True, wrap_lines=True,
                      completer=self._completer(), complete_while_typing=True,
                      auto_suggest=AutoSuggestFromHistory(),   # 历史 ghost 建议
                      history=self._history)
        frame = self._rounded_frame(ta)
        hint = Window(FormattedTextControl(lambda: self.status_fn()), height=1, style="class:hint")
        root = HSplit([frame, hint])
        kb = KeyBindings()

        @kb.add("enter", eager=True)
        def _(event):
            # 补全菜单已用 ↑↓ 选中某项 → Enter 接受补全；否则提交整段输入
            buf = ta.buffer
            if buf.complete_state and buf.complete_state.current_completion:
                buf.apply_completion(buf.complete_state.current_completion)
                return
            event.app.exit(result=ta.text)

        @kb.add("escape", "enter")   # Alt/Option+Enter
        @kb.add("c-j")               # Ctrl+J，部分终端的 Shift+Enter 也映射到这里
        def _(event):
            ta.buffer.insert_text("\n")

        @kb.add("right")             # →/Ctrl-E：光标在行尾且有历史建议时整段接受，否则正常右移
        @kb.add("c-e")
        def _(event):
            buf = ta.buffer
            if buf.suggestion and buf.suggestion.text and buf.cursor_position == len(buf.text):
                buf.insert_text(buf.suggestion.text)
            else:
                buf.cursor_right()

        @kb.add("c-c")
        @kb.add("c-d")
        def _(event):
            event.app.exit(result=EXIT)

        if self.mode_cycle_fn is not None:
            @kb.add("s-tab")     # Shift+Tab：循环 普通 → 自动接受编辑 → 计划模式 → 普通
            def _(event):
                self.mode_cycle_fn()
                event.app.invalidate()   # 底部状态行实时反映新模式

        style = Style.from_dict(self._style_dict())
        app = Application(layout=Layout(root), key_bindings=kb,
                          style=style, full_screen=False, mouse_support=False)
        result = app.run()
        if result is EXIT:
            return EXIT
        # 带框 Application 是 inline 渲染，退出后框会被擦除。把刚提交的输入回显成
        # 一行静态历史，让对话自上而下在框上方累积、输出不再“贴底”。
        self._echo_submitted(result or "")
        return result

    @staticmethod
    def _echo_submitted(text: str) -> None:
        if not text.strip():
            return
        marker = "❯" if os.environ.get("NO_COLOR") else "\033[36m❯\033[0m"
        lines = text.split("\n")
        # 首行带 ❯ 提示，多行输入的续行缩进 2 格与正文对齐
        body = f"{marker} {lines[0]}" + "".join(f"\n  {ln}" for ln in lines[1:])
        sys.stdout.write(body + "\n")
        sys.stdout.flush()

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
