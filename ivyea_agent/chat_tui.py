"""全屏 TUI 聊天界面（对标 Claude Code）。

分阶段构建（见计划）。P0：骨架——固定头部（当前指令置顶）+ 可滚动 transcript
+ 常驻底部输入框，能渲染与干净退出。完整对话闭环、流式、审批在后续阶段接入。

启用：TTY + IVYEA_TUI=1（opt-in）。非 TTY / 未开 / 依赖缺失时 `tui_enabled()`
返回 False，调用方回退到现有行式 CLI。
"""
from __future__ import annotations

import os
import sys
from typing import Callable

_TRUTHY = ("1", "true", "on", "yes")


def tui_enabled() -> bool:
    """是否走全屏 TUI。默认关（opt-in），P5 再翻默认。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if os.environ.get("IVYEA_TUI", "").strip().lower() not in _TRUTHY:
        return False
    try:
        import prompt_toolkit  # noqa: F401
    except Exception:
        return False
    return True


def _style():
    from prompt_toolkit.styles import Style
    return Style.from_dict({
        "hdr": "bold ansicyan",
        "rule": "ansibrightblack",
        "ftr": "noreverse ansibrightblack",
        "prompt": "ansicyan bold",
        "wip": "ansibrightblack",
    })


def run(status_fn: Callable[[], str], slash_commands: list) -> int:
    """P0 骨架 TUI。返回退出码（供 _cmd_chat 直接 return）。

    后续阶段会把 transcript 模型、后台跑轮、流式 marshal、审批浮层挂进来；
    P0 只验证全屏外壳（头部/transcript/输入）能渲染并干净退出。
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.widgets import TextArea
    from prompt_toolkit.key_binding import KeyBindings

    state = {
        "instruction": "",
        "lines": ["（P0 骨架：完整对话将在后续阶段接入。输入 /exit 退出。）"],
    }

    def header_text():
        instr = state["instruction"] or "（还没有指令）"
        return [("class:hdr", f" ▶ 当前指令：{instr}")]

    def body_text():
        return [("class:wip", "\n".join(state["lines"]))]

    def footer_text():
        return [("class:ftr", " " + (status_fn() or ""))]

    ta = TextArea(prompt=[("class:prompt", "❯ ")], multiline=False, height=1)
    root = HSplit([
        Window(FormattedTextControl(header_text), height=1, style="class:hdr"),
        Window(height=1, char="─", style="class:rule"),
        Window(FormattedTextControl(body_text), wrap_lines=True),   # transcript（P1 起可滚动）
        Window(height=1, char="─", style="class:rule"),
        Window(FormattedTextControl(footer_text), height=1, style="class:ftr"),
        ta,
    ])

    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("c-d")
    def _(event):
        event.app.exit(result=0)

    @kb.add("enter")
    def _(event):
        text = ta.text.strip()
        ta.text = ""
        if text in ("/exit", "/quit"):
            event.app.exit(result=0)
            return
        if text:
            state["instruction"] = text
            state["lines"].append(f"❯ {text}")
            state["lines"].append("（P0：对话闭环尚未接入，见 P1）")

    app = Application(
        layout=Layout(root, focused_element=ta),
        key_bindings=kb, style=_style(),
        full_screen=True, mouse_support=True,
    )
    app.run()
    return 0
