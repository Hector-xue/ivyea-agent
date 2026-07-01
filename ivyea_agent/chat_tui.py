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

_FALSY = ("0", "false", "off", "no")
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def tui_enabled() -> bool:
    """是否走全屏 TUI。默认开；`IVYEA_TUI=0/false/off/no` 退回行式 CLI。
    非 TTY / prompt_toolkit 不可用时也自动回退，保证任何环境可用。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if os.environ.get("IVYEA_TUI", "").strip().lower() in _FALSY:
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
                 render_markdown: Callable[[str], str] | None = None,
                 slash_commands: list | None = None,
                 plan_intent_fn: Callable[[str], str] | None = None,
                 set_plan_mode: Callable[[bool], str] | None = None,
                 cycle_mode: Callable[[], str] | None = None):
        self.status_fn = status_fn
        self.turn_fn = turn_fn
        self._md = render_markdown or (lambda s: s)
        self.slash_commands = slash_commands or []
        self._plan_intent = plan_intent_fn or (lambda _t: None)
        self._set_plan = set_plan_mode        # (on)->msg 或 None
        self._cycle = cycle_mode              # ()->label 或 None
        self.instruction = ""
        self.blocks: list[str] = []          # 已定稿 block（可含 ANSI）
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
            self.scroll = 0
            return "handled"
        pi = self._plan_intent(text)                       # 自然语言进/出计划模式
        if pi is not None:
            if self._set_plan is not None:
                self.blocks.append("\033[2m" + self._set_plan(pi == "enter") + "\033[0m")
            return "handled"
        if text.startswith("/"):                           # 其它斜杠命令：P5 再接全，先提示
            self.blocks.append(f"\033[2m（{text}：该命令暂请退出 TUI 后在行式界面使用；完整接入见 P5）\033[0m")
            return "handled"
        return "turn"

    def _body_ansi(self):
        from prompt_toolkit.formatted_text import ANSI
        parts = list(self.blocks)
        if self.live is not None:
            parts.append("\033[2m" + self.live + "\033[0m")   # 流式中 dim 显示原文
        if self.pending is not None:
            parts.append("\n".join(self._approval_lines()))   # 审批面板置于末尾
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
        if out.get("todos_panel"):           # 轮末计划面板（与行式对齐）
            self.blocks.append(out["todos_panel"])
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
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.styles import Style

        approving = Condition(lambda: self.pending is not None)

        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
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
                      auto_suggest=AutoSuggestFromHistory(), history=history)
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
                self.blocks.append(f"\033[2m⋯ 已排队：{text}\033[0m")
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
            self.blocks.append(f"\033[2m⇄ 模式：{label}\033[0m")
            self._invalidate()

        @kb.add("pageup")
        def _(event):
            self.scroll += 5

        @kb.add("pagedown")
        def _(event):
            self.scroll = max(0, self.scroll - 5)

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
        render_markdown: Callable[[str], str] | None = None,
        plan_intent_fn: Callable[[str], str] | None = None,
        set_plan_mode: Callable[[bool], str] | None = None,
        cycle_mode: Callable[[], str] | None = None) -> int:
    """启动 TUI 会话。turn_fn(line, render, narrate)->dict 跑真正一轮。"""
    tui = ChatTUI(status_fn=status_fn, turn_fn=turn_fn or (lambda *a, **k: {"text": ""}),
                  render_markdown=render_markdown, slash_commands=slash_commands,
                  plan_intent_fn=plan_intent_fn, set_plan_mode=set_plan_mode, cycle_mode=cycle_mode)
    from . import tui as _tui_mod
    _tui_mod.set_active_selector(tui._approve)   # 工具线程的审批 marshal 回本 app
    try:
        tui.build_app().run()
    finally:
        _tui_mod.set_active_selector(None)
    return 0
