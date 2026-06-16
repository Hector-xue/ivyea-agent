"""聊天输入框（prompt_toolkit）—— 支持粘贴、历史(↑↓)、斜杠命令下拉补全、底部状态栏。

无 prompt_toolkit 或非 TTY（管道/无终端）时优雅降级为内置 input()。
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from . import config


def build_session(slash_commands: list, status_fn: Callable[[], str]):
    """构造 PromptSession；不可用则返回 None（调用方回退 input()）。"""
    if not sys.stdin.isatty():
        return None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.key_binding import KeyBindings
    except Exception:
        return None

    class _SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            for cmd, desc in slash_commands:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text),
                                     display=cmd, display_meta=desc)

    config.ensure_dirs()
    kb = KeyBindings()

    @kb.add("c-j")  # Ctrl+J 插入换行（多行输入），Enter 仍发送
    def _(event):
        event.current_buffer.insert_text("\n")

    try:
        return PromptSession(
            history=FileHistory(str(config.IVYEA_DIR / "chat_history")),
            completer=_SlashCompleter(),
            complete_while_typing=True,
            auto_suggest=AutoSuggestFromHistory(),
            bottom_toolbar=lambda: status_fn(),
            key_bindings=kb,
            multiline=False,           # Enter 发送；粘贴多行由 bracketed paste 处理
            mouse_support=False,
        )
    except Exception:
        return None


def read(session, prompt_str: str, plain_prompt: str) -> str:
    """读一行输入。session 为 None 时回退 input()。"""
    if session is not None:
        try:
            from prompt_toolkit.formatted_text import ANSI
            return session.prompt(ANSI(prompt_str)).strip()
        except Exception:
            return input(plain_prompt).strip()
    return input(plain_prompt).strip()
