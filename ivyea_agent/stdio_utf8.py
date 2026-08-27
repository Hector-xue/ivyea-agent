"""把本进程的 stdout/stderr 钉死成 UTF-8。

**这是 Windows 上的救命护栏，不是洁癖。** 实测崩过一次（v1.15.16 之前）：

    File "ivyea_agent/service.py", line 2130, in run
      print(f"  {mark} {name}: ...")
    UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'

原因不在那个 ✓，在**流的编码**。Python 只有在 stdout 接的是真实控制台时才用
UTF-8；一旦被重定向到文件、管道或 NUL（IvyeaOps 起 serve 就是这么干的：
`stdout=日志文件` / `stdout=DEVNULL`），编码就退回系统 ANSI 代码页 ——
中文 Windows 是 GBK，英文 Windows 是 cp437。GBK 编不出 ✓，cp437 连中文都编不出，
而本项目的日志**通篇是中文**。于是 serve 崩在第一行输出上，端口没人监听，
IvyeaOps 那边只看得到一句 "All connection attempts failed"。

所以判据是：**凡是会 print 的入口，都先调一次 force_utf8()。**

为什么不改成 ASCII 符号：治不了。reason 文案本身就是中文，换掉 ✓ 只是把崩溃
从中文 Windows 挪到英文 Windows。

为什么不怕控制台乱码：真实控制台上这是个空操作 —— PEP 528 起 Python 在 Windows
控制台走的就是 UTF-8/UTF-16 那条路，reconfigure 到 UTF-8 改不动它的行为。
errors="replace" 是最后一道保险：宁可显示成 ? 也绝不让一个字符掀翻守护进程。
"""
from __future__ import annotations

import sys

_done = False


def force_utf8() -> None:
    """幂等；任何失败都咽掉 —— 它是护栏，不该自己变成新的崩溃点。"""
    global _done
    if _done:
        return
    _done = True
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        # 冻结的 GUI 子系统 exe 里这俩可能是 None；pytest 的捕获流没有 reconfigure。
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass
