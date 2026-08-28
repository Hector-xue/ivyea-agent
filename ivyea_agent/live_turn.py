"""活轮事件日志 —— 让"正在跑的这一轮"变成**服务端的事实**，而不是某个浏览器标签页的内存。

── 它解决的是什么 ────────────────────────────────────────────────────────────
`/v1/chat/stream` 是一条一次性的管子：事件写出去就没了。于是一轮任务的执行过程
**只存在于那一个发起它的页面里**。真实后果（用户实测反馈，逐条）：

  · 切到别的会话/板块再切回来 —— 执行过程整块消失，只剩自己发的那句话干挂着，
    "我知道后台在执行，但前端看不到进度了"；
  · 刷新同理，而且要等这一轮**整个跑完**、再刷新一次，才会"一下子把所有过程
    和结果输出出来"；
  · 换台机器打开同一条会话，什么都看不到。

轮末落盘（sessions.append_turn）救不了这个：一轮动辄几十分钟，那期间磁盘上什么
都没有。所以要有一份**跑的过程中就能被别人读到**的东西。

── 设计 ──────────────────────────────────────────────────────────────────────
每条会话最多一份活轮日志，跑完即封存（留一小会儿让刚接进来的客户端读到 final）。

两类东西分开存，这是内存能恒定的关键：

  · **正文**只存**一份累加字符串**（`text`）。token 事件一轮能有几万条，逐条存
    会把内存吃干；而客户端要的从来不是"第 8137 个 token"，是"现在正文长什么样"。
  · **结构化事件**（step / todos / answer_reset / final …）按序进环形队列。

那么正文和事件的**相对顺序**怎么保住？—— 每条事件记下"我被记录时正文有多长"
（`tlen`）。回放时先把正文补到 `tlen`，再发这条事件。于是
「说一段话 → 调两个工具 → 再说一段」的交错关系逐字还原，前端据此把一轮切成
分段汇报（和刷新后从存档里恢复出来的形态完全一致 —— 两边看到的必须是同一个东西）。

`answer_reset`（引证门打回重写）把 `text` 归零，事件的 tlen 也就是 0，跟随者
照做即可。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Iterator, Optional

from . import turn_inbox

#: 一轮最多留多少条结构化事件。192 步的长任务约 400~600 条；超出就丢最早的，
#: 并在回放时如实说"前面还有多少条没留下"，绝不假装完整。
MAX_EVENTS = 4000

#: 正文缓冲上限。超长报告截尾部 —— 回放只为让人看到进度，完整正文以 final 为准。
MAX_TEXT = 400_000

#: 轮次结束后日志还留多久（秒）。给"刚好在收尾那一刻接进来"的客户端一个窗口，
#: 让它能读到 final 而不是一片空白。
KEEP_AFTER_END = 300.0

#: 高频流（token / reasoning）不进事件队列，只更新缓冲。
_STREAMED = frozenset({"token", "reasoning"})


class LiveTurn:
    """一条会话正在跑的那一轮。线程安全 —— 写的是轮次线程，读的是若干个 SSE 连接。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.seq = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        #: 被环形队列挤掉的事件数。回放时照实报，不假装完整。
        self.dropped = 0
        self.text = ""
        self.reasoning = ""
        self.running = True
        self.started = time.time()
        self.ended = 0.0
        self.cond = threading.Condition()

    # ── 写端（轮次线程）────────────────────────────────────────────────────
    def record(self, event: str, data: dict[str, Any]) -> None:
        with self.cond:
            if event == "token":
                self.text = (self.text + str(data.get("text") or ""))[-MAX_TEXT:]
            elif event == "reasoning":
                # 思考只留最近一小段：活动行只显示一行，多存无用。
                self.reasoning = (self.reasoning + str(data.get("text") or ""))[-2000:]
            else:
                if event == "answer_reset":
                    self.text = ""      # 上一稿作废，跟随者也要跟着清
                if event == "final" and data.get("text"):
                    self.text = str(data.get("text"))[-MAX_TEXT:]
                self.seq += 1
                if len(self.events) == MAX_EVENTS:
                    self.dropped += 1
                self.events.append({
                    "seq": self.seq, "event": event, "data": data,
                    # 记录这一刻正文有多长 —— 回放时靠它把正文与事件的交错还原
                    "tlen": len(self.text),
                })
            self.cond.notify_all()

    def end(self) -> None:
        with self.cond:
            self.running = False
            self.ended = time.time()
            self.cond.notify_all()

    # ── 读端（每个 SSE 连接一个）──────────────────────────────────────────
    def follow(self, from_seq: int = 0, *, alive: Callable[[], bool],
               poll: float = 0.25) -> Iterator[tuple[str, dict[str, Any]]]:
        """从 `from_seq` 之后开始，先回放、再跟随。跟随到轮次结束为止。

        `alive()` 回 False（客户端断了）就收摊 —— 这条日志不属于任何一个连接，
        谁走了都不影响轮次本身，也不影响别的跟随者。
        """
        sent_seq = int(from_seq or 0)
        sent_len = 0
        last_out = time.time()
        # 断点续传时，客户端手上已有的正文长度由它自己带上来（见 service 里的
        # `text_from`）；这里从 0 起，由调用方决定要不要跳过开头那一段。
        first = True
        while True:
            with self.cond:
                if first:
                    first = False
                    snapshot = {
                        "running": self.running,
                        "seq": self.seq,
                        "dropped": self.dropped,
                        "started_ms": int(self.started * 1000),
                        "reasoning": self.reasoning,
                    }
                    yield ("live_begin", snapshot)
                pending = [e for e in self.events if e["seq"] > sent_seq]
                text = self.text
                running = self.running
                ended = self.ended
                if not pending and len(text) <= sent_len and running:
                    self.cond.wait(poll)
                    if not alive():
                        return
                    # 静默太久要吐个心跳。两个作用，缺一不可：中间代理不会把这条
                    # "看起来没在传数据"的连接掐掉；写失败也是我们唯一能察觉
                    # "客户端已经走了"的信号（HTTP 服务器不会主动告诉我们）。
                    if time.time() - last_out > 15.0:
                        last_out = time.time()
                        yield ("ping", {})
                    continue
            if pending or len(text) > sent_len:
                last_out = time.time()
            for ev in pending:
                # 先把正文补到这条事件发生时的长度，再发事件 —— 顺序就是这样保住的。
                want = min(int(ev.get("tlen") or 0), len(text))
                if want > sent_len:
                    yield ("token", {"text": text[sent_len:want]})
                    sent_len = want
                elif want < sent_len:
                    sent_len = want          # answer_reset 把正文清了
                yield (str(ev["event"]), dict(ev["data"]))
                sent_seq = int(ev["seq"])
            if len(text) > sent_len:
                yield ("token", {"text": text[sent_len:]})
                sent_len = len(text)
            if not alive():
                return
            if not running:
                # 轮次已收尾且事件全部发完 —— 再确认一次没有落下的，然后收摊。
                with self.cond:
                    if not [e for e in self.events if e["seq"] > sent_seq] and len(self.text) <= sent_len:
                        yield ("live_end", {"ended_ms": int((ended or time.time()) * 1000)})
                        return


_LIVE: dict[str, LiveTurn] = {}
_LOCK = threading.Lock()


def _sweep(now: float) -> None:
    """收尾超过 KEEP_AFTER_END 的日志清掉。惰性执行 —— 不为这个多开一个线程。"""
    for sid, live in list(_LIVE.items()):
        if not live.running and live.ended and now - live.ended > KEEP_AFTER_END:
            _LIVE.pop(sid, None)


def begin(session_id: str) -> LiveTurn:
    """开一轮。同一条会话再开一轮时，旧日志直接让位（一条会话同时只跑一轮）。"""
    now = time.time()
    with _LOCK:
        _sweep(now)
        old = _LIVE.get(session_id)
        if old is not None and old.running:
            old.end()          # 让上一轮的跟随者收摊，别永远挂着
        live = LiveTurn(session_id)
        _LIVE[session_id] = live
    # 上一轮没被读到的追加指令不该漏进这一轮：它是对**那一轮**说的话，
    # 而调用方在上一轮收尾时已经把它端走另做安排了（final 的 injected_pending）。
    turn_inbox.clear(session_id)
    return live


def get(session_id: str) -> Optional[LiveTurn]:
    with _LOCK:
        _sweep(time.time())
        return _LIVE.get(session_id)


def running_ids() -> list[str]:
    """此刻真的有一轮在跑的会话 id。

    读的是这个进程的内存，**不扫会话文件** —— 侧边栏要靠它每几秒问一次"哪几条
    在跑"，扫盘的实现放在那个频率上会把磁盘和 CPU 白白吃掉。
    """
    now = time.time()
    with _LOCK:
        _sweep(now)
        return [sid for sid, live in _LIVE.items() if live.running]


def status(session_id: str) -> dict[str, Any]:
    """给会话详情用的一行状态：这条会话现在有没有活轮。"""
    live = get(session_id)
    if live is None:
        return {"running": False, "seq": 0}
    return {
        "running": bool(live.running),
        "seq": int(live.seq),
        "started_ms": int(live.started * 1000),
    }


def reset_for_tests() -> None:
    with _LOCK:
        _LIVE.clear()
