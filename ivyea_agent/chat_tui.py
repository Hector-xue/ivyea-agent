"""全屏 TUI 聊天界面（对标 Claude Code）。

分阶段构建（见计划）。
- P0：骨架（头部/transcript/输入）。
- P1：核心闭环——提交回显 + sticky 头部 + 后台线程跑一轮 + 助手文本/工具行
  线程安全 marshal 进 transcript + 状态行 spinner + 贴底跟随/上滚查看。

启用：TTY + IVYEA_TUI=1（opt-in）。非 TTY / 未开 / 依赖缺失 → tui_enabled()
False，调用方回退现有行式 CLI。审批/补全/中断等见后续阶段。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from typing import Callable

_TRUTHY = ("1", "true", "on", "yes")
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


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


def _visual_lines(text: str, width: int) -> list[str]:
    """把（可能含 ANSI 的）文本按显示宽度粗略拆成物理行，用于贴底裁剪。"""
    from prompt_toolkit.utils import get_cwidth
    out: list[str] = []
    for logical in text.split("\n"):
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", logical)
        if get_cwidth(plain) <= width or width <= 0:
            out.append(logical)
            continue
        # 超宽行按显示宽度切（保留原行；仅用于行数估算，颜色可能在切点断，够用）
        cur, w = "", 0
        for ch in logical:
            cw = get_cwidth(ch) if not ch.startswith("\x1b") else 0
            if w + cw > width:
                out.append(cur); cur, w = ch, cw
            else:
                cur += ch; w += cw
        out.append(cur)
    return out


class ChatTUI:
    """一次 TUI 会话的状态与渲染。turn_fn(line, render, narrate)->dict 跑真正一轮。"""

    def __init__(self, *, status_fn: Callable[[], str], turn_fn: Callable[..., dict],
                 render_markdown: Callable[[str], str] | None = None):
        self.status_fn = status_fn
        self.turn_fn = turn_fn
        self._md = render_markdown or (lambda s: s)
        self.instruction = ""
        self.blocks: list[str] = []          # 已定稿 block（可含 ANSI）
        self.live: str | None = None         # 当前流式助手文本（未定稿）
        self.running = False
        self.cancel_requested = False        # Esc/Ctrl-C 请求中断当前轮
        self.queued: list[str] = []          # 运行中回车排队的后续指令
        self.scroll = 0                       # 距底部的物理行数；0=贴底跟随
        self.started = 0.0
        self.app = None

    # ---- 供后台线程调用的输出回调（线程安全：仅追加 + invalidate）----
    def render(self, token: str = "") -> None:
        if not token:
            return
        self.live = (self.live or "") + token
        self._invalidate()

    def narrate(self, text: str = "") -> None:
        text = str(text or "")
        if not text.strip():
            return
        self._commit_live()          # 工具行/提示打断当前流式文本，先定稿
        self.blocks.append(text)
        self._invalidate()

    def _commit_live(self) -> None:
        if self.live is not None:
            body = self.live.strip()
            if body:
                self.blocks.append("● " + self._md(body))   # 文本段收尾渲染 markdown
            self.live = None

    def _invalidate(self) -> None:
        app = self.app
        if app is not None:
            try:
                app.invalidate()
            except Exception:
                pass

    def _body_ansi(self):
        from prompt_toolkit.formatted_text import ANSI
        parts = list(self.blocks)
        if self.live is not None:
            parts.append("\033[2m" + self.live + "\033[0m")   # 流式中 dim 显示原文
        text = "\n\n".join(p for p in parts if p) or "（开始对话吧。输入 /exit 退出。）"
        width = shutil.get_terminal_size((100, 30)).columns
        rows = shutil.get_terminal_size((100, 30)).lines
        avail = max(3, rows - 5)                  # 头部+两条分隔+footer+输入 约 5 行
        lines = _visual_lines(text, width)
        if len(lines) > avail:
            end = len(lines) - self.scroll
            end = max(avail, min(end, len(lines)))
            lines = lines[end - avail:end]
        return ANSI("\n".join(lines))

    def _header(self):
        instr = self.instruction or "（还没有指令）"
        tail = "  ⟳ 运行中" if self.running else ""
        return [("class:hdr", f" ▶ 当前指令：{instr}{tail}")]

    def _footer(self):
        base = self.status_fn() or ""
        if self.running:
            frame = _SPIN[int((time.time() - self.started) * 10) % len(_SPIN)]
            secs = int(time.time() - self.started)
            q = f" · 已排队 {len(self.queued)}" if self.queued else ""
            hint = " · 中断中…" if self.cancel_requested else " · Esc/Ctrl-C 中断 · 回车排队下一条"
            return [("class:run", f" {frame} 生成中 {secs}s{hint}{q}"), ("class:ftr", " · " + base)]
        return [("class:ftr", " " + base)]

    def _start_turn(self, line: str) -> None:
        self.instruction = line
        self.blocks.append(f"\033[36m❯\033[0m {line}")
        self.scroll = 0
        self.running = True
        self.cancel_requested = False
        self.started = time.time()

        def _worker():
            try:
                out = self.turn_fn(line, self.render, self.narrate,
                                   cancel_check=lambda: self.cancel_requested)
            except KeyboardInterrupt:
                out = {"text": "", "cancelled": True}
            except TypeError:
                # turn_fn 不接受 cancel_check（如测试假函数）→ 退化重试
                try:
                    out = self.turn_fn(line, self.render, self.narrate)
                except Exception as e:   # noqa: BLE001
                    out = {"text": "", "error": str(e)}
            except Exception as e:   # noqa: BLE001
                out = {"text": "", "error": str(e)}
            self._finish(out)

        threading.Thread(target=_worker, daemon=True).start()
        self._invalidate()

    def _finish(self, out: dict) -> None:
        self._commit_live()
        if out.get("cancelled"):
            self.blocks.append("\033[33m⛔ 已中断（会话已保留，可继续输入）\033[0m")
        elif out.get("error"):
            self.blocks.append(f"\033[31m✗ 出错：{out['error']}\033[0m")
        self.running = False
        self.cancel_requested = False
        self.scroll = 0
        # 处理运行中排队的后续指令：逐条自动继续
        if self.queued:
            nxt = self.queued.pop(0)
            self._invalidate()
            self._start_turn(nxt)
            return
        self._invalidate()

    def build_app(self):
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.widgets import TextArea
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        ta = TextArea(prompt=[("class:prompt", "❯ ")], multiline=False, height=1)
        root = HSplit([
            Window(FormattedTextControl(self._header), height=1, style="class:hdr"),
            Window(height=1, char="─", style="class:rule"),
            Window(FormattedTextControl(self._body_ansi), wrap_lines=True),
            Window(height=1, char="─", style="class:rule"),
            Window(FormattedTextControl(self._footer), height=1),
            ta,
        ])
        kb = KeyBindings()

        @kb.add("c-c")
        def _(event):
            if self.running:                 # 运行中：请求中断当前轮
                self.cancel_requested = True
            else:                            # 空闲：退出
                event.app.exit(result=0)

        @kb.add("c-d")
        def _(event):
            event.app.exit(result=0)

        @kb.add("escape", eager=True)
        def _(event):
            if self.running:                 # 运行中：Esc 也请求中断
                self.cancel_requested = True

        @kb.add("enter")
        def _(event):
            text = ta.text.strip()
            ta.text = ""
            if not text:
                return
            if self.running:                 # 运行中：回车把指令排队，本轮结束后自动继续
                if text in ("/exit", "/quit"):
                    self.cancel_requested = True
                    event.app.exit(result=0)
                    return
                self.queued.append(text)
                self.blocks.append(f"\033[2m⋯ 已排队：{text}\033[0m")
                self._invalidate()
                return
            if text in ("/exit", "/quit"):
                event.app.exit(result=0)
                return
            self._start_turn(text)

        @kb.add("pageup")
        def _(event):
            self.scroll += 5

        @kb.add("pagedown")
        def _(event):
            self.scroll = max(0, self.scroll - 5)

        style = Style.from_dict({
            "hdr": "bold ansicyan", "rule": "ansibrightblack",
            "ftr": "noreverse ansibrightblack", "run": "ansiyellow",
            "prompt": "ansicyan bold",
        })
        self.app = Application(
            layout=Layout(root, focused_element=ta), key_bindings=kb, style=style,
            full_screen=True, mouse_support=True, refresh_interval=0.2,
        )
        return self.app


def run(status_fn: Callable[[], str], slash_commands: list,
        turn_fn: Callable[..., dict] | None = None,
        render_markdown: Callable[[str], str] | None = None) -> int:
    """启动 TUI 会话。turn_fn(line, render, narrate)->dict 跑真正一轮。"""
    tui = ChatTUI(status_fn=status_fn, turn_fn=turn_fn or (lambda *a, **k: {"text": ""}),
                  render_markdown=render_markdown)
    tui.build_app().run()
    return 0
