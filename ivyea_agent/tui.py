"""交互式终端选择器（prompt_toolkit）。

审批等场景用 ↑/↓ + Enter 选项，而非手输数字 —— 对标 Codex/Claude/Hermes。
三级降级：tty + prompt_toolkit → 交互菜单；否则带编号的 input()；EOF/Ctrl-C → abort。
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from . import ui


def _default_input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _fallback(title: str, body: str, options: list[tuple[str, str]],
              kind: str, input_fn: Callable[[str], str]) -> str:
    """带编号的 input() 兜底；返回选中的 key（无效/EOF → 最后一项，约定 abort）。"""
    keys = [k for k, _ in options]
    print()
    print(ui.panel(title, body, kind=kind))
    menu = "  ".join(f"[{i + 1}]{lbl}" for i, (_, lbl) in enumerate(options))
    for _ in range(6):
        raw = input_fn("选择 " + menu + ": ").strip().lower()
        if raw.isdigit():
            i = int(raw) - 1
            if 0 <= i < len(options):
                return keys[i]
        if raw in keys:
            return raw
        if raw == "":
            return keys[-1]
        print(ui.message("warn", f"请输入 1-{len(options)}。"))
    return keys[-1]


def _interactive(title: str, body: str, options: list[tuple[str, str]], kind: str) -> str:
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style

    state = {"idx": 0}
    n = len(options)

    def render():
        frags = [("class:title", f"  ▌ {title}\n")]
        for line in str(body).splitlines() or [""]:
            frags.append(("class:dim", f"    {line}\n"))
        frags.append(("", "\n"))
        for i, (_, lbl) in enumerate(options):
            if i == state["idx"]:
                frags.append(("class:sel", f"  ❯ {lbl}\n"))
            else:
                frags.append(("class:opt", f"    {lbl}\n"))
        frags.append(("class:hint", "  ↑/↓ 选择 · Enter 确认 · 数字直选 · Ctrl-C 全部停止"))
        return frags

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(_e):
        state["idx"] = (state["idx"] - 1) % n

    @kb.add("down")
    @kb.add("j")
    def _(_e):
        state["idx"] = (state["idx"] + 1) % n

    @kb.add("enter")
    def _(e):
        e.app.exit(result=options[state["idx"]][0])

    @kb.add("c-c")
    @kb.add("c-d")
    def _(e):
        e.app.exit(result=options[-1][0])   # 约定最后一项 = abort/全部停止

    def _make_digit(idx):
        def handler(e):
            e.app.exit(result=options[idx][0])
        return handler

    for d in range(1, min(n, 9) + 1):
        kb.add(str(d))(_make_digit(d - 1))

    win = Window(FormattedTextControl(render, focusable=True), wrap_lines=True)
    style = Style.from_dict({
        "title": "bold ansiyellow",
        "sel": "bold ansicyan reverse",
        "opt": "",
        "dim": "ansibrightblack",
        "hint": "ansibrightblack",
    })
    app = Application(layout=Layout(HSplit([win])), key_bindings=kb, style=style,
                      full_screen=False, mouse_support=False)
    return app.run()


def select(title: str, body: str, options: list[tuple[str, str]], *,
           kind: str = "warn", input_fn: Optional[Callable[[str], str]] = None) -> str:
    """渲染一个单选菜单，返回选中的 option key。

    options = [(key, label), ...]，约定最后一项是 abort/停止（Ctrl-C/EOF 落到它）。
    tty + prompt_toolkit 用 ↑/↓ + Enter；否则降级到带编号的 input()。
    """
    if not options:
        return ""
    reader = input_fn or _default_input
    if not sys.stdin.isatty():
        return _fallback(title, body, options, kind, reader)
    try:
        return _interactive(title, body, options, kind)
    except Exception:
        return _fallback(title, body, options, kind, reader)
