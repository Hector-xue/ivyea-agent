"""全屏 TUI 聊天界面（对标 Claude Code）。

分阶段构建（见计划）。
- P0：骨架（头部/transcript/输入）。
- P1：核心闭环——提交回显 + sticky 头部 + 后台线程跑一轮 + 助手文本/工具行
  线程安全 marshal 进 transcript + 状态行 spinner + 贴底跟随/上滚查看。

默认启用（TTY 下）。`IVYEA_TUI=0/false/off/no` 退回行式 CLI；非 TTY / 依赖缺失
时 tui_enabled() 返回 False 自动回退。功能：sticky 头部 + 常驻输入 + 后台流式 +
中断/排队 + TUI 内审批 + 补全/历史/计划模式/Shift+Tab + 轮末 todo 面板。
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
_FALSY = ("0", "false", "off", "no")
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def tui_enabled() -> bool:
    """是否走全屏 alt-screen TUI（输入框彻底钉死、翻历史也在）。**默认开**。
    `IVYEA_TUI=0/false/off/no` 或 `IVYEA_LIVE=1`（要滚动区）→ 关；
    非 TTY / prompt_toolkit 不可用时也回退。"""
    if os.environ.get("IVYEA_LIVE", "").strip().lower() in _TRUTHY:
        return False
    if os.environ.get("IVYEA_TUI", "").strip().lower() in _FALSY:
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        import prompt_toolkit  # noqa: F401
    except Exception:
        return False
    return True


def _visual_lines(text: str, width: int) -> list[str]:
    """按显示宽度把（含 ANSI 的）文本拆成物理行，用于精确的末屏裁剪（贴底）。"""
    from prompt_toolkit.utils import get_cwidth
    out: list[str] = []
    for logical in text.split("\n"):
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", logical)
        if width <= 0 or get_cwidth(plain) <= width:
            out.append(logical); continue
        cur, w, in_esc = "", 0, False
        for ch in logical:
            if ch == "\x1b":
                in_esc = True
            if in_esc:
                cur += ch
                if ch.isalpha():
                    in_esc = False
                continue
            cw = get_cwidth(ch)
            if w + cw > width:
                out.append(cur); cur, w = ch, cw
            else:
                cur += ch; w += cw
        out.append(cur)
    return out


def _make_scroll_control(get_text, on_scroll):
    """FormattedTextControl + 鼠标滚轮处理（alt-screen 下 transcript 被裁到末屏、内容不溢出，
    需自己接滚轮）。SCROLL_UP/DOWN → on_scroll(delta)。Shift+拖选仍由终端原生处理（复制）。"""
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.mouse_events import MouseEventType

    class _C(FormattedTextControl):
        def mouse_handler(self, mouse_event):
            et = mouse_event.event_type
            if et == MouseEventType.SCROLL_UP:
                on_scroll(-3); return None
            if et == MouseEventType.SCROLL_DOWN:
                on_scroll(3); return None
            return super().mouse_handler(mouse_event)

    return _C(get_text)


def _ptprint(s: str) -> None:
    """打印带 ANSI 的一段到滚动缓冲区。patch_stdout 下必须走 print_formatted_text(ANSI())，
    直接 print 原始 ANSI 会被当字面量（显示成 ?[36m）。"""
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import ANSI
    print_formatted_text(ANSI(s))


class _ScrollEmitter:
    """滚动缓冲区输出器（patch_stdout 安全、无光标 erase）。assistant 文本按块（空行边界）缓冲
    → 渲染 markdown 打印；工具行/思考直接打印。对标 Claude/Ink：完成内容落到正常滚动缓冲区，
    底部输入框由 app 常驻重绘。所有输出走 _ptprint（print_formatted_text(ANSI)）。"""
    def __init__(self, render_markdown):
        self._md = render_markdown or (lambda s: s)
        self._buf = ""            # 当前 assistant 文本块缓冲
        self._emitted = False     # 本轮是否已定稿过块（● 仅首块）
        self._reason_buf = ""      # 思考缓冲（收尾/被打断时一次性 dim 打印）

    def text(self, token: str) -> None:
        self._buf += token
        from . import markdown
        blocks, self._buf = markdown.split_stream_blocks(self._buf)
        for block in blocks:
            self._emit(block)

    def _emit(self, block: str) -> None:
        if not block.strip():
            return
        self._flush_reason()
        prefix = "\033[36m●\033[0m " if not self._emitted else ""
        self._emitted = True
        _ptprint(prefix + self._md(block.strip()))

    def flush_text(self) -> None:
        if self._buf.strip():
            self._emit(self._buf)
        self._buf = ""

    def line(self, text: str) -> None:
        """工具行 / stage / 提示：先把当前文本块定稿，再直接打印。"""
        self.flush_text()
        self._flush_reason()
        _ptprint(text)

    def echo(self, line: str) -> None:
        """把用户指令回显成 Claude 风格灰底带（打到滚动缓冲区）。"""
        import shutil
        try:
            from prompt_toolkit.utils import get_cwidth
            dw = lambda s: sum(get_cwidth(c) for c in s)   # noqa: E731
        except Exception:
            dw = len
        width = max(20, shutil.get_terminal_size((80, 24)).columns)
        BG, MK, FG = "\033[48;5;236m", "\033[38;5;45m", "\033[38;5;252m"
        for i, ln in enumerate(str(line).split("\n")):
            head = f"{MK}> {FG}" if i == 0 else f"{FG}  "
            pad = " " * max(0, width - 2 - dw(ln))
            _ptprint(f"{BG}{head}{ln}{pad}\033[0m")

    def reasoning(self, token: str) -> None:
        self._reason_buf += token or ""

    def _flush_reason(self) -> None:
        if self._reason_buf.strip():
            _ptprint("\033[2m✻ 思考\n" + self._reason_buf.strip() + "\033[0m")
        self._reason_buf = ""


class ChatTUI:
    """一次 TUI 会话的状态与渲染。turn_fn(line, render, narrate)->dict 跑真正一轮。"""

    def __init__(self, *, status_fn: Callable[[], str], turn_fn: Callable[..., dict],
                 render_markdown: Callable[[str], str] | None = None,
                 slash_commands: list | None = None,
                 plan_intent_fn: Callable[[str], str] | None = None,
                 set_plan_mode: Callable[[bool], str] | None = None,
                 cycle_mode: Callable[[], str] | None = None,
                 mode_label_fn: Callable[[], str] | None = None,
                 slash_handlers: dict | None = None,
                 scrollback: bool = False,
                 intro: str | None = None):
        self.status_fn = status_fn
        self.turn_fn = turn_fn
        self._md = render_markdown or (lambda s: s)
        self.slash_commands = slash_commands or []
        self._slash_handlers = slash_handlers or {}   # {"/model": fn, ...} 全量斜杠命令
        self.scrollback = scrollback                  # True=常驻底部 app+patch_stdout（默认体验）
        self._emitter = _ScrollEmitter(self._md) if scrollback else None
        self._mode_label_fn = mode_label_fn   # ()->当前模式文字 或 None
        self._plan_intent = plan_intent_fn or (lambda _t: None)
        self._set_plan = set_plan_mode        # (on)->msg 或 None
        self._cycle = cycle_mode              # ()->label 或 None
        self.instruction = ""
        self.blocks: list[str] = [intro] if intro else []   # banner+欢迎框作首块
        self.live: str | None = None         # 当前流式助手文本（未定稿）
        self.running = False
        self.cancel_requested = False        # Esc/Ctrl-C 请求中断当前轮
        self.queued: list[str] = []          # 运行中回车排队的后续指令
        self.scroll = 0                       # 距底部的物理行数；0=贴底跟随
        self.started = 0.0
        self.app = None
        # 审批（P3）：工具线程投递请求→主 app 渲染内联浮层→选定回填并唤醒
        self.pending = None                  # {"title","body","options","idx"}
        self._approval_ev = None
        self._approval_result = None

    # ---- 供后台线程调用的输出回调（线程安全：仅追加 + invalidate）----
    def render(self, token: str = "") -> None:
        if not token:
            return
        if self.scrollback:          # 打到正常滚动缓冲区上方（输入框由 app 常驻钉底）
            self._emitter.text(token)
            return
        self.live = (self.live or "") + token
        self._scroll_to_bottom()     # 新内容 → 贴底跟随
        self._invalidate()

    def render_reasoning(self, token: str = "") -> None:
        if self.scrollback and token:
            self._emitter.reasoning(token)

    def _emit_line(self, text: str) -> None:
        """一次性提示行（排队/模式切换/斜杠提示）：滚动区 print，alt-screen 进 transcript。"""
        if self.scrollback:
            _ptprint(text)
        else:
            self.blocks.append(text)
            self._scroll_to_bottom()
            self._invalidate()

    def narrate(self, text: str = "") -> None:
        text = str(text or "")
        if not text.strip():
            return
        if self.scrollback:
            self._emitter.line(text)
            return
        self._commit_live()          # 工具行/提示打断当前流式文本，先定稿
        self.blocks.append(text)
        self._scroll_to_bottom()
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

    # ---- 审批（工具线程调用，阻塞等主 app 选定）----
    def _approve(self, title: str, body: str, options: list, kind: str = "warn") -> str:
        ev = threading.Event()
        self._approval_ev = ev
        self._approval_result = None
        self.pending = {"title": title, "body": body, "options": options, "idx": 0}
        self._invalidate()
        ev.wait()                      # 阻塞工具线程，直到主 app 选定
        return self._approval_result or (options[-1][0] if options else "")

    def _confirm_approval(self, idx: int) -> None:
        opts = self.pending["options"] if self.pending else []
        if opts:
            self._approval_result = opts[max(0, min(idx, len(opts) - 1))][0]
        self.pending = None
        if self._approval_ev is not None:
            self._approval_ev.set()
        self._invalidate()

    def _approval_lines(self) -> list[str]:
        p = self.pending
        out = [f"\033[33m  ▌ {p['title']}\033[0m"]
        for ln in str(p["body"]).splitlines():
            out.append("    " + ln)
        out.append("")
        for i, (_k, label) in enumerate(p["options"]):
            mark = "\033[36m ❯ \033[0m" if i == p["idx"] else "   "
            out.append(f"{mark}{i + 1}. {label}")
        out.append("\033[2m  ↑/↓ 选择 · Enter 确认 · 数字直选 · Ctrl-C 停止\033[0m")
        return out

    def _completer(self):
        from prompt_toolkit.completion import Completer, Completion
        slash = self.slash_commands

        class _C(Completer):
            def get_completions(s, document, complete_event):
                t = document.text_before_cursor
                if not t.startswith("/"):
                    return
                for cmd, desc in slash:
                    if cmd.startswith(t):
                        yield Completion(cmd, start_position=-len(t), display=cmd, display_meta=desc)
        return _C()

    def _handle_submit(self, text: str) -> str:
        """处理一条已提交文本。返回 'exit' / 'handled' / 'turn'。"""
        if text in ("/exit", "/quit"):
            return "exit"
        if text == "/clear":
            self.blocks = []
            self.live = None
            self._scroll_to_bottom()
            if self.scrollback:
                self._emit_line("\033[2m（已清空对话上下文）\033[0m")
            return "handled"
        pi = self._plan_intent(text)                       # 自然语言进/出计划模式
        if pi is not None:
            if self._set_plan is not None:
                self._emit_line("\033[2m" + self._set_plan(pi == "enter") + "\033[0m")
            return "handled"
        head = text.split()[0] if text.split() else text
        handler = self._slash_handlers.get(head)           # 全量斜杠命令（/model /compact /rewind /paste …）
        if handler is not None:
            self._run_slash(handler, text)
            return "handled"
        if text.startswith("/"):                           # 未识别的斜杠命令 → 当普通轮/展开交给 turn_fn
            return "turn"
        return "turn"

    # 交互式斜杠命令（handler 内部会弹 tui.select 需要真实终端）：这些挂起 app 到终端跑；
    # 其余纯输出命令（/help /status /cost /tools /diff …）捕获 print 进 transcript，不用
    # run_in_terminal——手机 web 终端(xterm.js)下 run_in_terminal 会输出丢失+乱码。
    _INTERACTIVE_SLASH = {"/model", "/config", "/paste", "/mcp", "/update", "/rewind", "/profile"}

    def _run_slash(self, handler, text: str) -> None:
        """执行斜杠命令。纯输出命令：捕获 handler 的 print → 走 _emit_line 进 transcript
        （不依赖 run_in_terminal/CPR，兼容 web 终端；输入框不消失、长输出可滚）。
        交互命令：run_in_terminal 挂起 app 到真实终端跑（需终端交互）。"""
        import io
        from . import tui as _tui_mod
        head = text.split()[0] if text.split() else text

        if head not in self._INTERACTIVE_SLASH:
            buf = io.StringIO()
            saved = sys.stdout
            try:
                sys.stdout = buf
                handler(text)
            except Exception as e:   # noqa: BLE001
                buf.write(f"\033[31m命令出错：{e}\033[0m")
            finally:
                sys.stdout = saved
            out = buf.getvalue().rstrip("\n")
            if out.strip():
                self._emit_line(out)
            else:
                self._invalidate()
            return

        # 交互命令：挂起 app 到真实终端（run_in_terminal），set_active_selector(None) 让
        # handler 的 tui.select 走终端交互而非 marshal 到已挂起的主 app（会死锁）。
        def _call():
            _tui_mod.set_active_selector(None)
            saved = sys.stdout
            try:
                if sys.__stdout__ is not None:
                    sys.stdout = sys.__stdout__
                handler(text)
            finally:
                sys.stdout = saved
                _tui_mod.set_active_selector(self._approve)
        try:
            import asyncio
            from prompt_toolkit.application import run_in_terminal
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # Unit tests and non-running fallback callers have an event-loop
                # object, but it is not running.  Scheduling there leaves a
                # pending coroutine that is never awaited; run synchronously.
                _call()
            else:
                run_in_terminal(_call)
        except Exception:
            try:
                _call()
            except Exception as e:   # noqa: BLE001
                self._emit_line(f"\033[31m命令出错：{e}\033[0m")

    def _body_height(self) -> int:
        """transcript 可用物理行数（终端高 - 输入框上线/输入/下线/状态栏 4 行）。"""
        rows = shutil.get_terminal_size((100, 30)).lines
        return max(3, rows - 4)

    def _body_ansi(self):
        from prompt_toolkit.formatted_text import ANSI
        parts = list(self.blocks)
        if self.live is not None:
            parts.append("\033[2m" + self.live + "\033[0m")   # 流式中 dim 显示原文
        # 思考中（还没吐字 / 工具间隙）：把"生成中"放在内容即将出现的位置（transcript 末尾）
        elif self.running and self.pending is None:
            frame = _SPIN[int((time.time() - self.started) * 10) % len(_SPIN)]
            secs = int(time.time() - self.started)
            parts.append(f"\033[2m{frame} 生成中 {secs}s…\033[0m")
        if self.pending is not None:
            parts.append("\n".join(self._approval_lines()))   # 审批面板置于末尾
        text = "\n\n".join(p for p in parts if p) or "（开始对话吧。输入 /exit 退出。）"
        # 按显示宽度裁到"末屏 - scroll"：scroll=0 显示底部(贴底跟随)，PgUp/滚轮增大 scroll 看更早
        width = shutil.get_terminal_size((100, 30)).columns
        lines = _visual_lines(text, width)
        avail = self._body_height()
        self._max_scroll = max(0, len(lines) - avail)
        self.scroll = max(0, min(self.scroll, self._max_scroll))
        if len(lines) > avail:
            end = len(lines) - self.scroll
            lines = lines[end - avail:end]
        return ANSI("\n".join(lines))

    def _scroll_by(self, delta: int) -> None:
        """滚动 transcript：delta<0 上滚(看更早)，>0 下滚(回到更新)。"""
        self.scroll = max(0, min(getattr(self, "_max_scroll", 0), self.scroll - delta))
        self._invalidate()

    def _scroll_to_bottom(self) -> None:
        """新内容出现时贴底跟随。"""
        self.scroll = 0

    def _mode_label(self):
        """输入框上边线右端的模式标签（计划模式/自动接受编辑），对标 Claude。"""
        m = self._mode_label_fn() if self._mode_label_fn else ""
        if not m:
            return [("class:rule", "──")]
        return [("class:rule", "── "), ("class:mode", f" {m} "), ("class:rule", "──")]

    def _footer(self):
        base = self.status_fn() or ""
        if self.running:
            q = f" · 已排队 {len(self.queued)}" if self.queued else ""
            hint = "中断中…" if self.cancel_requested else "Esc/Ctrl-C 中断 · 回车排队下一条"
            if self.scrollback:   # 滚动区模式：footer 是唯一的"生成中"指示（带转圈+耗时）
                frame = _SPIN[int((time.time() - self.started) * 10) % len(_SPIN)]
                secs = int(time.time() - self.started)
                return [("class:run", f" {frame} 生成中 {secs}s · {hint}{q}"), ("class:ftr", " · " + base)]
            return [("class:run", f" {hint}{q}"), ("class:ftr", " · " + base)]
        return [("class:ftr", " " + base)]

    def _start_turn(self, line: str) -> None:
        self.instruction = line
        if self.scrollback:                              # 指令回显成 Claude 风格灰底带，打到滚动区
            self._emitter.echo(line)
        else:
            self.blocks.append(f"\033[36m❯\033[0m {line}")   # 内联回显（alt-screen transcript）
        self._scroll_to_bottom()
        self.running = True
        self.cancel_requested = False
        self.started = time.time()

        def _call(**extra):
            return self.turn_fn(line, self.render, self.narrate, **extra)

        def _worker():
            # 逐步退化匹配 turn_fn 签名（测试假函数可能不接受新 kwargs）；每级都兜住中断/异常
            for extra in ({"cancel_check": lambda: self.cancel_requested,
                           "render_reasoning": self.render_reasoning},
                          {"cancel_check": lambda: self.cancel_requested},
                          {}):
                try:
                    out = _call(**extra)
                except KeyboardInterrupt:
                    out = {"text": "", "cancelled": True}
                except TypeError:
                    continue   # 签名不匹配 → 试更少的参数
                except Exception as e:   # noqa: BLE001
                    out = {"text": "", "error": str(e)}
                self._finish(out)
                return
            self._finish({"text": "", "error": "turn_fn 签名不兼容"})

        threading.Thread(target=_worker, daemon=True).start()
        self._invalidate()

    def _finish(self, out: dict) -> None:
        if self.scrollback:
            self._emitter.flush_text()               # 定稿最后一个文本块
            self._emitter._flush_reason()
            if out.get("todos_panel"):
                _ptprint(out["todos_panel"])
            if out.get("cancelled"):
                _ptprint("\033[33m⛔ 已中断（会话已保留，可继续输入）\033[0m")
            elif out.get("error"):
                _ptprint(f"\033[31m✗ 出错：{out['error']}\033[0m")
            self._emitter._emitted = False           # 下一轮重新从 ● 首块开始
        else:
            self._commit_live()
            if out.get("todos_panel"):           # 轮末计划面板（与行式对齐）
                self.blocks.append(out["todos_panel"])
            if out.get("cancelled"):
                self.blocks.append("\033[33m⛔ 已中断（会话已保留，可继续输入）\033[0m")
            elif out.get("error"):
                self.blocks.append(f"\033[31m✗ 出错：{out['error']}\033[0m")
        self.running = False
        self.cancel_requested = False
        self._scroll_to_bottom()
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
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.styles import Style

        approving = Condition(lambda: self.pending is not None)

        from .chat_input import slash_aware_autosuggest
        history = None
        try:
            from prompt_toolkit.history import FileHistory
            from . import config as _cfg
            _cfg.ensure_dirs()
            history = FileHistory(str(_cfg.IVYEA_DIR / "chat_history"))
        except Exception:
            history = None
        ta = TextArea(prompt=[("class:prompt", "❯ ")], multiline=False, height=1,
                      completer=self._completer(), complete_while_typing=True,
                      auto_suggest=slash_aware_autosuggest(self.slash_commands), history=history)   # 斜杠 ghost=补全菜单第一项
        from prompt_toolkit.layout.containers import VSplit, ConditionalContainer
        from prompt_toolkit.formatted_text import ANSI
        # 输入框上边线：右端显示当前模式（计划模式/自动接受编辑），对标 Claude 边线上的标签
        top_border = VSplit([
            Window(height=1, char="─", style="class:rule"),                  # 左侧铺满的线
            Window(FormattedTextControl(self._mode_label), height=1, dont_extend_width=True),
        ])
        # 审批面板（有 pending 时显示在输入框上方；两模式共用）
        approval_win = ConditionalContainer(
            Window(FormattedTextControl(lambda: ANSI("\n".join(self._approval_lines()))), wrap_lines=True),
            filter=approving)
        if self.scrollback:
            # 常驻底部 app：无内部 transcript（输出经 patch_stdout 落到正常滚动缓冲区上方）
            self._body_window = None
            root = HSplit([
                approval_win,
                top_border,                                                  # 输入框上边线（右端带模式）
                ta,                                                          # 输入框（钉底）
                Window(height=1, char="─", style="class:rule"),             # 输入框下边线
                Window(FormattedTextControl(self._footer), height=1),        # 状态栏（生成中也在）
            ])
        else:
            # alt-screen：内部 transcript（末屏裁剪+贴底+键盘/鼠标滚轮滚动；审批面板在 _body_ansi 末尾）
            self._body_window = Window(_make_scroll_control(self._body_ansi, self._scroll_by), wrap_lines=True)
            root = HSplit([
                self._body_window,
                top_border,
                ta,
                Window(height=1, char="─", style="class:rule"),
                Window(FormattedTextControl(self._footer), height=1),
            ])
        kb = KeyBindings()

        @kb.add("c-c")
        def _(event):
            if self.pending is not None:     # 审批中：Ctrl-C = 选最后一项(约定=停止)
                self._confirm_approval(len(self.pending["options"]) - 1)
            elif self.running:               # 运行中：请求中断当前轮
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
            if self.pending is not None:     # 审批中：回车确认当前选项（下方 filtered 处理，此处兜底）
                self._confirm_approval(self.pending["idx"])
                return
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
                self._emit_line(f"\033[2m⋯ 已排队：{text}\033[0m")
                self._invalidate()
                return
            action = self._handle_submit(text)   # /exit /clear /plan / 计划模式 NL / 其它斜杠 / 普通轮
            if action == "exit":
                event.app.exit(result=0)
            elif action == "handled":
                self._invalidate()
            else:
                self._start_turn(text)

        @kb.add("s-tab")                     # Shift+Tab：循环 普通/自动接受编辑/计划模式
        def _(event):
            if self.running or self.pending is not None or self._cycle is None:
                return
            label = self._cycle()
            self._emit_line(f"\033[2m⇄ 模式：{label}\033[0m")
            self._invalidate()

        # 键盘滚动 transcript（mouse_support=False 为了原生框选复制，滚动走键盘）。
        # PgUp/PgDn + Ctrl-U/Ctrl-B（上）、Ctrl-N（下）（浏览器终端 PgUp 可能被拦）。
        @kb.add("pageup")
        @kb.add("c-u")
        @kb.add("c-b")
        def _(event): self._scroll_by(-8)     # 上滚看更早

        @kb.add("pagedown")
        @kb.add("c-n")
        def _(event): self._scroll_by(8)      # 下滚回到更新

        # Tab：有补全菜单则循环候选（斜杠/@，所见即所得），否则接受历史 ghost 建议
        @kb.add("tab")
        def _(event):
            buf = ta.buffer
            if buf.complete_state:
                buf.complete_next()
            elif buf.suggestion and buf.suggestion.text:
                buf.insert_text(buf.suggestion.text)
            elif buf.text.startswith("/"):
                buf.start_completion(select_first=True)

        # →（行尾）也接受 ghost 建议，对齐行式界面的习惯
        @kb.add("right")
        def _(event):
            buf = ta.buffer
            if buf.cursor_position == len(buf.text) and buf.suggestion and buf.suggestion.text:
                buf.insert_text(buf.suggestion.text)
            else:
                buf.cursor_position += 1

        # 审批中：↑/↓ 移动选择，数字直选（仅 pending 时激活，不影响正常输入）
        @kb.add("up", filter=approving)
        def _(event):
            n = len(self.pending["options"])
            self.pending["idx"] = (self.pending["idx"] - 1) % n
            self._invalidate()

        @kb.add("down", filter=approving)
        def _(event):
            n = len(self.pending["options"])
            self.pending["idx"] = (self.pending["idx"] + 1) % n
            self._invalidate()

        def _mk_digit(d):
            def handler(event):
                if self.pending and d - 1 < len(self.pending["options"]):
                    self._confirm_approval(d - 1)
            return handler

        for _d in range(1, 10):
            kb.add(str(_d), filter=approving)(_mk_digit(_d))

        style = Style.from_dict({
            "rule": "ansibrightblack", "ftr": "noreverse ansibrightblack",
            "run": "ansiyellow", "prompt": "ansicyan bold",
            "mode": "ansicyan bold", "auto-suggestion": "ansibrightblack",
        })
        # alt-screen：mouse_support=True → 鼠标滚轮滚 transcript；Shift+拖选仍走终端原生选中(复制)。
        # scrollback：mouse_support=False → 完全交给终端(原生滚轮+框选复制)，full_screen=False。
        self.app = Application(
            layout=Layout(root, focused_element=ta), key_bindings=kb, style=style,
            full_screen=not self.scrollback, mouse_support=not self.scrollback, refresh_interval=0.3,
        )
        return self.app


def run(status_fn: Callable[[], str], slash_commands: list,
        turn_fn: Callable[..., dict] | None = None,
        render_markdown: Callable[[str], str] | None = None,
        plan_intent_fn: Callable[[str], str] | None = None,
        set_plan_mode: Callable[[bool], str] | None = None,
        cycle_mode: Callable[[], str] | None = None,
        mode_label_fn: Callable[[], str] | None = None,
        slash_handlers: dict | None = None,
        scrollback: bool = False,
        intro: str | None = None) -> int:
    """启动 TUI 会话。scrollback=True → 常驻底部 app + 输出落正常滚动缓冲区（默认体验，
    保留原生滚轮/复制）；False → alt-screen 全屏 TUI。turn_fn 跑真正一轮。"""
    tui = ChatTUI(status_fn=status_fn, turn_fn=turn_fn or (lambda *a, **k: {"text": ""}),
                  render_markdown=render_markdown, slash_commands=slash_commands,
                  plan_intent_fn=plan_intent_fn, set_plan_mode=set_plan_mode, cycle_mode=cycle_mode,
                  mode_label_fn=mode_label_fn, slash_handlers=slash_handlers,
                  scrollback=scrollback, intro=intro)
    from . import tui as _tui_mod
    _tui_mod.set_active_selector(tui._approve)   # 工具线程的审批 marshal 回本 app
    if scrollback and intro:
        _ptprint(intro)                          # banner/欢迎框打到滚动缓冲区（输入框在其下方钉底）
    try:
        if scrollback:
            from prompt_toolkit.patch_stdout import patch_stdout
            with patch_stdout():                 # 让后台/工具线程的 print 落在输入框上方
                tui.build_app().run()
        else:
            tui.build_app().run()
    finally:
        _tui_mod.set_active_selector(None)
    return 0
