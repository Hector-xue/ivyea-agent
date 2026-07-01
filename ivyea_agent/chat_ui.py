"""chat 对话模式的纯展示层 helper（从 cli.py 拆出，降低 god-file 体量）。

只含无状态/自包含的工具：域判定、终端宽度、生成 spinner、完整流式打印器。
不依赖 cli 内部，避免循环导入；颜色码本地定义一份。
"""
from __future__ import annotations

import os
import re
import select
import shutil
import sys
import time

_C = {"g": "\033[32m", "c": "\033[36m", "d": "\033[2m", "b": "\033[1m", "x": "\033[0m"}


# 亚马逊广告域信号：命中任一则本轮按广告任务处理，注入内置知识/skill。
_AMAZON_TERMS = (
    "广告", "否词", "否定", "竞价", "出价", "bid", "预算", "budget", "acos", "acoas", "roas",
    "tacos", "asin", "listing", "关键词", "搜索词", "投放", "活动", "campaign", "浪费词",
    "转化", "点击", "曝光", "ctr", "cpc", "cvr", "店铺", "销量", "库存", "类目", "竞品",
    "sp广告", "sb广告", "sd广告", "亚马逊", "amazon", "运营", "领星", "lingxing",
    "sku", "review", "qa", "offer",
)


def _is_amazon_domain(query: str) -> bool:
    """本轮是否带亚马逊广告/运营域信号。无信号的工程任务据此跳过内置知识注入。"""
    q = (query or "").lower()
    return any(t in q for t in _AMAZON_TERMS)


# 代码/工程任务信号：源码文件后缀 + 代码语义词。比 engineering_context 的术语表更宽，
# 用来兜住「给 calc.py 加 docstring」这类不含'优化/bug'但分明是写代码的请求。
_CODE_HINTS = re.compile(
    r"\.(py|js|ts|tsx|jsx|go|rs|java|kt|rb|php|cs|cpp|cc|hpp|sh|sql|ya?ml|toml|ini|css|html?|vue)\b"
    r"|docstring|traceback|stack ?trace|函数|方法|变量|类型|形参|参数列表|报错|异常|栈|仓库|repo\b"
    r"|分支|commit|merge|pull ?request|\bdef \b|\bclass \b|\bimport \b|编译|断点|单元测试|代码|脚本",
    re.I,
)


def _looks_like_code_task(query: str) -> bool:
    """本轮是否是写代码/工程任务（用于决定是否跳过亚马逊知识注入）。"""
    from . import engineering_context
    return engineering_context.should_include(query) or bool(_CODE_HINTS.search(query or ""))


def _dwidth(s: str) -> int:
    """终端显示宽度（CJK 全角算 2 格），用于状态行不换行截断。"""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)


class _LiveSpinner:
    """生成时的实时反馈。

    对标 Claude Code / Codex 的终端观感：运行状态不再直接占据最后一行输入位，
    而是渲染为「状态行 + 底部只读 composer 占位」。这样用户始终能看到底部
    输入区位置和中断提示；真正输入仍在本轮结束/中断后恢复。
    设置 IVYEA_BUSY_COMPOSER=0 可回退为旧的单行 spinner。
    """
    _F = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, *, busy_composer: bool | None = None):
        self.i = 0
        self.on = False
        self.start = None
        self.chars = 0
        self.tail = ""   # 当前行尾的正文预览
        self.input_preview = ""
        if busy_composer is None:
            busy_composer = os.environ.get("IVYEA_BUSY_COMPOSER", "").strip().lower() not in (
                "0", "false", "off", "no"
            )
        self.busy_composer = busy_composer

    @staticmethod
    def _clip(text: str, width: int) -> str:
        text = str(text or "")
        if width <= 0:
            return ""
        clipped = False
        while _dwidth(text) > width and len(text) > 1:
            text = text[:-1]
            clipped = True
        return text + ("…" if clipped and width > 1 else "")

    def _status_body(self) -> tuple[str, str]:
        frame = self._F[self.i % len(self._F)]
        elapsed = int(time.time() - (self.start or time.time()))
        toks = self.chars // 3   # 中英混排粗估 ~3 字符/token，仅作进度感
        tok_s = f"~{toks / 1000:.1f}k tok" if toks >= 1000 else f"~{toks} tok"
        head = f"生成中 {elapsed}s · {tok_s} · "
        width = shutil.get_terminal_size((80, 24)).columns
        avail = width - 2 - _dwidth(head) - 1   # 2=frame+空格，1=余量
        preview = self.tail.strip()
        if preview and avail > 6:
            clipped = False
            while _dwidth(preview) > avail - 1 and len(preview) > 1:
                preview = preview[1:]; clipped = True   # 从左裁，保留最新文字
            body = head + ("…" + preview if clipped else preview)
        else:
            body = head + "Ctrl-C 中断"
        return frame, body

    def set_input_preview(self, text: str = "") -> None:
        self.input_preview = str(text or "")

    def tick(self, text: str = "") -> None:
        if self.start is None:
            self.start = time.time()
        if text:
            self.chars += len(text)
            combined = self.tail + text
            if "\n" in combined:
                combined = combined.rsplit("\n", 1)[1]   # 只留最后一行
            self.tail = combined
        self.i += 1
        frame, body = self._status_body()
        if not self.busy_composer:
            sys.stdout.write(f"\r\033[K{_C['c']}{frame}{_C['x']} {_C['d']}{body}{_C['x']}")
        else:
            width = shutil.get_terminal_size((80, 24)).columns
            composer = self._clip("╭─ 任务运行中 · Ctrl-C/Esc 中断 · 输入文字回车排队", width)
            prompt_text = f"╰─ ❯ {self.input_preview}" if self.input_preview else "╰─ ❯ "
            prompt = self._clip(prompt_text, width)
            if self.on:
                sys.stdout.write("\r\033[K\033[1B\r\033[K\033[1A\r")
            sys.stdout.write(
                f"\r\033[K{_C['c']}{frame}{_C['x']} {_C['d']}{body}{_C['x']}\n"
                f"\033[K{_C['d']}{composer}\n{prompt}{_C['x']}\033[1A\r"
            )
        sys.stdout.flush()
        self.on = True

    def clear(self) -> None:
        if self.on:
            if self.busy_composer:
                sys.stdout.write("\r\033[K\033[1B\r\033[K\033[1B\r\033[K\033[2A\r")
            else:
                sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self.on = False
            self.tail = ""   # 一段叙述/工具行后重置预览
            self.input_preview = ""


class BusyInput:
    """Best-effort background input while a turn is running.

    It intentionally avoids taking over the full prompt_toolkit Application used by
    ChatInput. In TTY mode it watches stdin with select/termios and supports:
    - Ctrl-C / Esc: request cancellation of the current turn.
    - Enter after typing text: queue that text as the next instruction.

    In non-TTY or unsupported terminals it becomes a no-op, so existing tests and
    pipes keep their old behavior.
    """

    def __init__(self, *, enabled: bool | None = None, on_change=None):
        if enabled is None:
            enabled = os.environ.get("IVYEA_BUSY_INPUT", "").strip().lower() not in (
                "0", "false", "off", "no"
            )
        self.enabled = enabled and sys.stdin.isatty()
        self.cancelled = False
        self.queued: list[str] = []
        self._buf = ""
        self.on_change = on_change or (lambda _text: None)
        self._old_term = None
        self._active = False

    def __enter__(self):
        if not self.enabled:
            return self
        try:
            import termios
            import tty
            self._old_term = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
            self._active = True
        except Exception:
            self.enabled = False
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        if self._active and self._old_term is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_term)
            except Exception:
                pass
        self._active = False
        return False

    def poll(self) -> None:
        if not self.enabled or self.cancelled:
            return
        try:
            while select.select([sys.stdin], [], [], 0)[0]:
                ch = sys.stdin.read(1)
                if not ch:
                    return
                if ch in ("\x03", "\x1b"):
                    self.cancelled = True
                    return
                if ch in ("\r", "\n"):
                    text = self._buf.strip()
                    self._buf = ""
                    self.on_change("")
                    if text:
                        self.queued.append(text)
                        self.on_change("已排队：" + text)
                    return
                if ch in ("\x7f", "\b"):
                    self._buf = self._buf[:-1]
                    self.on_change(self._buf)
                elif ch.isprintable():
                    self._buf += ch
                    self.on_change(self._buf)
        except Exception:
            return

    def cancel_check(self) -> bool:
        self.poll()
        return self.cancelled

    def pop_queued(self) -> str:
        return self.queued.pop(0) if self.queued else ""


class _StreamPrinter:
    """完整流式：边生成边打印正文（dim），收尾按视觉行数擦除最后一段再渲染 markdown。
    仅在 tty + /stream on 时启用；任何异常/非 tty 由调用方回退到 spinner 路径。"""
    def __init__(self):
        self.block = ""   # 当前连续正文段（被工具行打断即提交清零）
        self.on = False

    def render(self, text: str = "") -> None:
        if not text:
            return
        sys.stdout.write(f"{_C['d']}{text}{_C['x']}")
        sys.stdout.flush()
        self.block += text
        self.on = True

    def commit(self) -> None:
        """被 narrate（工具行/提示）打断：保留已打印文本，重置当前段。"""
        self.block = ""

    @staticmethod
    def _visual_lines(s: str, width: int) -> int:
        n = 0
        for seg in s.split("\n"):
            w = _dwidth(seg)
            n += max(1, -(-w // width)) if w else 1
        return n

    def rerender(self, final_text: str) -> None:
        """擦除最后一段流式正文，改打印 markdown 渲染版。"""
        from . import markdown
        if self.on and self.block:
            width = shutil.get_terminal_size((80, 24)).columns
            lines = self._visual_lines(self.block, width)
            sys.stdout.write("\r")
            if lines > 1:
                sys.stdout.write(f"\033[{lines - 1}A")
            sys.stdout.write("\033[J")
            sys.stdout.flush()
        print(f"{_C['c']}●{_C['x']} " + markdown.render(final_text))
