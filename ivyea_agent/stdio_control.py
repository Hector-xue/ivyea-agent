"""`ivyea chat -p --input-format stream-json` 的控制通道：stdin 进、stdout 出。

── 为什么要有它 ──────────────────────────────────────────────────────────────
`-p` 是一次性子进程：调用方喂一句话、读一串 NDJSON、进程退出。中间那几十分钟里，
**没有任何一条路能从调用方走回来** —— stdin 是关着的。后果有三：

  · 想在轮次跑着的时候补一句话，做不到（IvyeaOps 的 /agents 聊天就卡在这）；
  · `ask_user_question` 弹不出选项卡（没有通道 → 立刻按推荐项走，等于没问）；
  · 想中止只能 SIGTERM 整个进程 —— **这一轮跑出来的东西一个字都不落盘**，
    因为 `_persist()` 根本来不及跑。

serve（常驻 HTTP）那边这三件事都有解（`/v1/chat/inject`、`/question`、`/cancel`），
但那是另一个进程里的注册表，隔着进程边界够不着。所以 `-p` 需要自己的通道。

── 协议 ──────────────────────────────────────────────────────────────────────
形状对齐 Claude Code 的 stdio control protocol（IvyeaOps 的 claude_driver 已经在
说这套话，消费方少学一套）：

  调用方 → 进程（stdin，一行一个 JSON）
    {"type":"user_input","text":"顺便把预算也看一下"}      追加指令
    {"type":"control_response","request_id":"…","response":{"answers":{…}}}  选项卡的答案
    {"type":"interrupt"}                                   中止这一轮（优雅收摊）

  进程 → 调用方（stdout，混在既有的 NDJSON 事件流里）
    {"type":"control_request","request_id":"…","request":{"subtype":"ask_user_question",…}}

**读 stdin 的是一个守护线程**：轮次线程在跑模型/工具，不能被读阻塞；而 stdin 上
什么时候来消息完全取决于人。线程读到就往内存里放，轮次线程在自己的安全点（步边界、
模型流的每个事件）来取。
"""
from __future__ import annotations

import json
import queue
import secrets
import sys
import threading
import time
from typing import Any, Optional


class StdioControl:
    """一次 `-p` 运行的控制通道。没开 `--input-format stream-json` 时压根不构造。"""

    def __init__(self, emit) -> None:
        #: 往 stdout 写一行 NDJSON（复用 stream_json.emit_line，保证和事件流同一把锁/同一格式）
        self._emit = emit
        self._lock = threading.Lock()
        self._inbox: list[dict[str, Any]] = []          # 待插入的追加指令
        self._pending: dict[str, "queue.Queue[dict]"] = {}   # request_id → 等答案的槽
        self._cancelled = threading.Event()
        self._closed = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def start(self, stream=None) -> "StdioControl":
        self._thread = threading.Thread(target=self._read_loop, args=(stream or sys.stdin,),
                                        name="ivyea-stdio-control", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._closed.set()
        # 还挂着等答案的，一律放行成"没人答"——调用方走了，谁也不会再回了。
        with self._lock:
            slots = list(self._pending.values())
        for slot in slots:
            try:
                slot.put_nowait({})
            except queue.Full:
                pass

    # ── 读端（守护线程）────────────────────────────────────────────────────
    def _read_loop(self, stream) -> None:
        try:
            for raw in stream:
                if self._closed.is_set():
                    return
                line = (raw or "").strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, TypeError):
                    continue        # 半截 JSON / 噪音：跳过这一行，别把通道带崩
                self._handle(msg)
        except Exception:  # noqa: BLE001 —— stdin 断了就是调用方走了，不是错误
            return

    def _handle(self, msg: dict) -> None:
        kind = str(msg.get("type") or "")
        if kind == "user_input":
            text = str(msg.get("text") or "").strip()
            if text:
                with self._lock:
                    self._inbox.append({"id": str(msg.get("id") or secrets.token_hex(6)),
                                        "text": text, "ts": time.time()})
            return
        if kind == "interrupt":
            self._cancelled.set()
            return
        if kind == "control_response":
            request_id = str(msg.get("request_id") or "")
            with self._lock:
                slot = self._pending.get(request_id)
            if slot is None:
                return              # 已经超时收摊了：迟到的答案直接丢，不改变已发生的事
            response = msg.get("response")
            try:
                slot.put_nowait(response if isinstance(response, dict) else {})
            except queue.Full:
                pass

    # ── 写端（轮次线程在安全点调用）────────────────────────────────────────
    def drain(self) -> list[dict[str, Any]]:
        """取走待插入的追加指令（agent_loop 在步边界调用）。"""
        with self._lock:
            items, self._inbox = self._inbox, []
        return items

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def ask(self, questions: list[dict], timeout_s: float) -> Optional[dict]:
        """ask.AskFn 的 stdio 实现：发 control_request → 等 control_response。

        超时/调用方断开都返回 None —— 由 `ask.resolve` 收敛到"按推荐项继续"。
        分段等待而不是一次 get(timeout)：调用方中途走了（stdin 关了）就尽早收摊，
        没必要在一个没人会回的问题上把整轮挂满五分钟。
        """
        request_id = secrets.token_hex(8)
        slot: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = slot
        deadline = time.time() + float(timeout_s)
        try:
            self._emit({
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "ask_user_question", "questions": questions,
                            "timeout_s": float(timeout_s), "expires_at": deadline},
            })
            while True:
                try:
                    got = slot.get(timeout=0.5)
                except queue.Empty:
                    if self._closed.is_set() or self._cancelled.is_set():
                        return None
                    if time.time() >= deadline:
                        self._emit({"type": "control_request", "request_id": request_id,
                                    "request": {"subtype": "ask_user_question_timeout"}})
                        return None
                    continue
                return got or None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
