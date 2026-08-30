"""`-p --input-format stream-json` 的控制通道：stdin 进（追加指令/答案/中止）、stdout 出。

为什么这条通道必须存在：`-p` 是一次性子进程，中间那几十分钟里调用方**没有任何一条路
走回来**。于是 IvyeaOps 的 /agents 聊天里，ivyea 这一档既插不进话、也弹不出选项卡，
想停只能 SIGTERM —— 而那样这一轮跑出来的东西一个字都不会落盘。
"""
from __future__ import annotations

import io
import json
import threading
import time

from ivyea_agent.stdio_control import StdioControl


def _pipe(lines: list[str]) -> io.StringIO:
    return io.StringIO("".join(l + "\n" for l in lines))


def test_user_input_lands_in_the_inbox():
    sent: list = []
    ctrl = StdioControl(sent.append).start(_pipe([
        json.dumps({"type": "user_input", "text": "顺便把预算也看一下"}),
        json.dumps({"type": "user_input", "text": "  "}),          # 空的不算
        "不是 JSON 的一行",                                          # 噪音不该带崩通道
    ]))
    for _ in range(100):
        if ctrl.drain.__self__._inbox:                              # noqa: SLF001 — 等读线程
            break
        time.sleep(0.01)
    items = ctrl.drain()
    assert [i["text"] for i in items] == ["顺便把预算也看一下"]
    assert ctrl.drain() == []                                       # 取走即消费
    ctrl.close()


def test_interrupt_sets_the_cancel_flag():
    ctrl = StdioControl(lambda _e: None).start(_pipe([json.dumps({"type": "interrupt"})]))
    for _ in range(100):
        if ctrl.cancelled():
            break
        time.sleep(0.01)
    assert ctrl.cancelled() is True
    ctrl.close()


def test_ask_round_trip():
    """选项卡：发 control_request → 调用方回 control_response → 拿到答案。"""
    sent: list = []
    reader = io.StringIO()
    ctrl = StdioControl(sent.append)

    # 手工喂一条答案：先拿到 request_id，再按它回
    def answer_later():
        for _ in range(200):
            if sent:
                rid = sent[0]["request_id"]
                ctrl._handle({"type": "control_response", "request_id": rid,     # noqa: SLF001
                              "response": {"answers": {"口径按哪个？": "环比"}}})
                return
            time.sleep(0.01)

    ctrl.start(reader)
    t = threading.Thread(target=answer_later)
    t.start()
    got = ctrl.ask([{"question": "口径按哪个？", "options": [{"label": "环比"}, {"label": "同比"}]}], 5)
    t.join()

    assert got == {"answers": {"口径按哪个？": "环比"}}
    assert sent[0]["type"] == "control_request"
    assert sent[0]["request"]["subtype"] == "ask_user_question"
    ctrl.close()


def test_ask_times_out_and_says_so():
    """没人答：返回 None（由 ask.resolve 收敛到"按推荐项继续"），并回一条超时事件。"""
    sent: list = []
    ctrl = StdioControl(sent.append).start(io.StringIO(""))
    started = time.time()
    assert ctrl.ask([{"question": "q", "options": [{"label": "A"}]}], 0.6) is None
    assert time.time() - started < 3
    assert [e["request"]["subtype"] for e in sent] == [
        "ask_user_question", "ask_user_question_timeout"]
    ctrl.close()


def test_closing_the_channel_releases_a_waiting_ask():
    """调用方走了（stdin 关了）就别再等 —— 五分钟挂在一个没人会回的问题上是纯浪费。"""
    ctrl = StdioControl(lambda _e: None).start(io.StringIO(""))
    box: dict = {}

    def asker():
        box["out"] = ctrl.ask([{"question": "q", "options": [{"label": "A"}]}], 300)

    t = threading.Thread(target=asker)
    t.start()
    time.sleep(0.3)
    started = time.time()
    ctrl.close()
    t.join(timeout=5)
    assert box.get("out") in (None, {})
    assert time.time() - started < 5


def test_late_answer_after_timeout_is_ignored():
    """迟到的答案不该改变已经发生的事（那一步早按推荐项走下去了）。"""
    sent: list = []
    ctrl = StdioControl(sent.append).start(io.StringIO(""))
    assert ctrl.ask([{"question": "q", "options": [{"label": "A"}]}], 0.5) is None
    rid = sent[0]["request_id"]
    ctrl._handle({"type": "control_response", "request_id": rid,        # noqa: SLF001
                  "response": {"answers": {"q": "A"}}})                  # 不抛、不改变什么
    ctrl.close()
