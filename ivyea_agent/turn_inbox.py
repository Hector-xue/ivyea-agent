"""正在跑的那一轮的「追加指令收件箱」。

── 它解决的是什么 ────────────────────────────────────────────────────────────
一轮任务动辄几十分钟。此前那几十分钟里用户是**闭麦**的：工作台的发送键在轮次
跑起来之后整个变成「停止」，想补一句"顺便把 X 也改了"，只能干等到收尾、或者
掐掉重说一遍。而人最想补话的时刻恰恰是看着它跑偏的那一刻。

收件箱让这句话能**进到正在跑的这一轮里**：HTTP 端点把文本投进来，轮次线程在
两个工具步之间排空它，作为一条真实的 user 消息追加进上下文。模型下一步就看得见。

── 为什么是"收件箱"而不是直接改 messages ─────────────────────────────────────
投递方是 HTTP 线程，消费方是轮次线程。直接去动那份 messages 列表，等于让两个
线程改同一个对象，而且很可能改在**不能插入的位置**上：assistant(tool_calls) 与
它的 tool 结果之间插一条 user 消息，provider 会直接拒掉整轮。所以投递只管排队，
插入时机由消费方（agent_loop 的步边界）说了算。

没被消费的条目不会丢：轮次收尾时 `drain_remaining` 把它们端出来，调用方（serve）
在 final 里回报给前端，由前端当成下一轮发出去。宁可晚一轮，也不能无声吞掉一句
用户说过的话。
"""
from __future__ import annotations

import threading
import time
import secrets
from typing import Any

#: 一轮最多接多少条追加指令。到顶就明确拒绝并说明 —— 不静默丢，
#: 用户得知道自己那句话到底进没进去。
MAX_PENDING = 20

#: 单条长度上限。追加指令是"补一句"，不是重新提交一份需求文档。
MAX_TEXT = 8000

_BOXES: dict[str, list[dict[str, Any]]] = {}
_LOCK = threading.Lock()


def submit(session_id: str, text: str) -> dict[str, Any]:
    """投一条追加指令进这条会话的收件箱。

    只管排队，不判断这条会话有没有活轮 —— 那是调用方（HTTP 端点）该先查的，
    因为"没有活轮"要回给用户的是"这条会存成下一轮"，而不是一个失败。
    """
    sid = str(session_id or "").strip()
    body = str(text or "").strip()
    if not sid:
        return {"ok": False, "error": "session_id is required"}
    if not body:
        return {"ok": False, "error": "text is required"}
    if len(body) > MAX_TEXT:
        return {"ok": False, "error": "text_too_long",
                "detail": f"追加指令最长 {MAX_TEXT} 字，这条有 {len(body)} 字。"}
    item = {"id": secrets.token_hex(6), "text": body, "ts": time.time()}
    with _LOCK:
        box = _BOXES.setdefault(sid, [])
        if len(box) >= MAX_PENDING:
            return {"ok": False, "error": "inbox_full",
                    "detail": f"这一轮已经排了 {len(box)} 条还没被读到，先等它消化。"}
        box.append(item)
        depth = len(box)
    return {"ok": True, "item": dict(item), "pending": depth}


def drain(session_id: str) -> list[dict[str, Any]]:
    """排空并返回这条会话待消费的追加指令（轮次线程在步边界调用）。"""
    with _LOCK:
        box = _BOXES.get(str(session_id or ""))
        if not box:
            return []
        items, box[:] = list(box), []
        return items


def pending(session_id: str) -> list[dict[str, Any]]:
    """看一眼还有几条没被读到（不消费）。"""
    with _LOCK:
        return [dict(i) for i in _BOXES.get(str(session_id or ""), [])]


def drain_remaining(session_id: str) -> list[dict[str, Any]]:
    """轮次收尾：把没来得及消费的端出来并清空。调用方负责让它们不落空。"""
    items = drain(session_id)
    clear(session_id)
    return items


def clear(session_id: str) -> None:
    with _LOCK:
        _BOXES.pop(str(session_id or ""), None)


def reset_for_tests() -> None:
    with _LOCK:
        _BOXES.clear()
