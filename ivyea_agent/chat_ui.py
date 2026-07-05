"""chat 对话模式的纯展示层 helper（从 cli.py 拆出，降低 god-file 体量）。

只含无状态/自包含的工具：域判定、终端宽度、生成 spinner、完整流式打印器。
不依赖 cli 内部，避免循环导入；颜色码本地定义一份。
"""
from __future__ import annotations

import re
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
    """生成时的实时反馈：转圈 + 耗时 + 估算 token + **正在生成的正文末尾预览**（单行 \\r 覆盖，零闪烁）。
    不打印完整正文，收尾仍统一渲染 markdown。"""
    _F = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self):
        self.i = 0
        self.on = False
        self.start = None
        self.chars = 0
        self.tail = ""   # 当前行尾的正文预览

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
        frame = self._F[self.i % len(self._F)]
        elapsed = int(time.time() - self.start)
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
        sys.stdout.write(f"\r\033[K{_C['c']}{frame}{_C['x']} {_C['d']}{body}{_C['x']}")
        sys.stdout.flush()
        self.on = True

    def clear(self) -> None:
        if self.on:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self.on = False
            self.tail = ""   # 一段叙述/工具行后重置预览


class _StreamPrinter:
    """渐进式 markdown 流式：正文以 dim 逐字出字；每当一个块（空行分隔）完成，立即擦掉
    该 dim 块、改打印其 markdown 渲染版（committed），继续流下一块——收尾只剩最后一块要转，
    避免整段"原文→渲染"的大跳变。仅 tty + /stream 时启用；异常/非 tty 由调用方回退 spinner。"""
    def __init__(self):
        self.block = ""       # 屏上底部当前未定稿的 dim 块
        self.on = False
        self._emitted = False  # 本轮是否已定稿过块（决定 ● 前缀只加一次）

    def render(self, text: str = "") -> None:
        if not text:
            return
        sys.stdout.write(f"{_C['d']}{text}{_C['x']}")
        sys.stdout.flush()
        self.block += text
        self.on = True
        from . import markdown
        completed_blocks, remainder = markdown.split_stream_blocks(self.block)
        if completed_blocks:
            self._erase(self.block)                # 擦掉屏上整块 dim（completed+remainder）
            for completed in completed_blocks:
                self._emit_md(completed)
            self.block = remainder
            if remainder:                          # 余下部分继续以 dim 挂在底部
                sys.stdout.write(f"{_C['d']}{remainder}{_C['x']}")
                sys.stdout.flush()

    def commit(self) -> None:
        """被 narrate（工具行/提示）打断：把当前块定稿为 markdown 再让工具行打印其下。"""
        if self.block.strip():
            self._erase(self.block)
            self._emit_md(self.block)
        self.block = ""

    @staticmethod
    def _visual_lines(s: str, width: int) -> int:
        n = 0
        for seg in s.split("\n"):
            w = _dwidth(seg)
            n += max(1, -(-w // width)) if w else 1
        return n

    def _erase(self, text: str) -> None:
        if not (self.on and text):
            return
        width = shutil.get_terminal_size((80, 24)).columns
        lines = self._visual_lines(text, width)
        sys.stdout.write("\r")
        if lines > 1:
            sys.stdout.write(f"\033[{lines - 1}A")
        sys.stdout.write("\033[J")
        sys.stdout.flush()

    def _emit_md(self, block: str) -> None:
        from . import markdown
        prefix = f"{_C['c']}●{_C['x']} " if not self._emitted else ""   # ● 仅首个定稿块
        self._emitted = True
        print(prefix + markdown.render(block.strip()))

    def rerender(self, final_text: str) -> None:
        """轮末：把最后一个未定稿块转成 markdown；若整段无空行则此处一次性渲染。"""
        if self.block.strip():
            self._erase(self.block)
            self._emit_md(self.block)
        elif not self._emitted:                    # 从没定稿过（如空回复）→ 兜底渲染全文
            from . import markdown
            print(f"{_C['c']}●{_C['x']} " + markdown.render(final_text))
        self.block = ""


class _ReasoningPrinter:
    """思考流（支持 reasoning 的模型）：dim 灰的 ✻ 思考 块，正文开始前收尾。"""
    def __init__(self):
        self.on = False

    def render(self, text: str = "") -> None:
        if not text:
            return
        if not self.on:
            sys.stdout.write(f"\n{_C['d']}✻ 思考\n")
            self.on = True
        sys.stdout.write(f"{_C['d']}{text}{_C['x']}")
        sys.stdout.flush()

    def done(self) -> None:
        if self.on:
            sys.stdout.write(f"{_C['x']}\n")
            sys.stdout.flush()
            self.on = False
