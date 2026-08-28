"""逐轮时间账：落盘、轮号对齐、以及详情分页里的投影。

为什么要落盘而不是让前端自己掐表：一轮跑完，工作台会重新去拉存档（那是"刷新
之后执行过程还在"的同一条路），前端内存里记的时长会被那次拉取冲掉；换台机器、
换个标签页打开同一条会话更是一个数都没有。

轮号的口径必须和 `transcript.turn_slices`（详情按轮分页用的那个）**同源**：
都数"第几条真实用户消息"。用下标或"落盘过几批"都会在压缩过/导入过/并发写过的
会话上错位，而错位的时间戳比没有时间戳更糟 —— 它看着是真的。
"""
from __future__ import annotations

import time

from ivyea_agent import transcript


def _sessions(ivyea_home):
    import importlib
    from ivyea_agent import sessions
    return importlib.reload(sessions)


def test_turn_times_survive_a_second_append(ivyea_home):
    sessions = _sessions(ivyea_home)
    sid = sessions.new_id()
    # 开跑：先落用户那句话，再记起点（chat_stream 就是这个顺序）
    sessions.append_turn(sid, "sys", [{"role": "user", "content": "做 A"}])
    idx = sessions.current_turn_index(sid)
    assert idx == 0
    sessions.note_turn_time(sid, idx, started_at=1000.0)
    # 收尾：补结束时刻和时长
    sessions.append_turn(sid, "sys", [{"role": "assistant", "content": "做完了"}])
    sessions.note_turn_time(sid, idx, ended_at=1180.0, ms=180_000)

    times = sessions.turn_times(sid)
    assert times == [{"turn": 0, "started_at": 1000.0, "ended_at": 1180.0, "ms": 180_000}]


def test_second_turn_gets_its_own_row(ivyea_home):
    sessions = _sessions(ivyea_home)
    sid = sessions.new_id()
    for n, (start, end) in enumerate([(10.0, 20.0), (30.0, 45.0)]):
        sessions.append_turn(sid, "sys", [{"role": "user", "content": f"第 {n} 问"}])
        idx = sessions.current_turn_index(sid)
        sessions.note_turn_time(sid, idx, started_at=start)
        sessions.append_turn(sid, "sys", [{"role": "assistant", "content": "答"}])
        sessions.note_turn_time(sid, idx, ended_at=end, ms=int((end - start) * 1000))

    times = sessions.turn_times(sid)
    assert [t["turn"] for t in times] == [0, 1]
    assert [t["ms"] for t in times] == [10_000, 15_000]


def test_turn_number_matches_the_paging_slices(ivyea_home):
    """轮号与详情分页的切法必须对得上 —— 追加指令自成一轮时也一样。"""
    sessions = _sessions(ivyea_home)
    sid = sessions.new_id()
    sessions.append_turn(sid, "sys", [{"role": "user", "content": "做 A"}])
    main = sessions.current_turn_index(sid)
    sessions.append_turn(sid, "sys", [
        {"role": "assistant", "content": "在做"},
        {"role": "user", "content": "[用户追加指令] 顺便把标题也改了"},
        {"role": "assistant", "content": "都做完了"},
    ])
    injected = sessions.current_turn_index(sid)
    assert (main, injected) == (0, 1)

    data = sessions.load(sid)
    kept = transcript.strip_injected(data["messages"])
    slices = transcript.turn_slices(kept)
    assert len(slices) == 2          # 主轮 + 追加指令那一轮
    # 第 n 个切片 = 第 n 轮：时间账挂上去之后不会张冠李戴
    a, b = slices[injected]
    assert "顺便把标题也改了" in kept[a]["content"]
    assert b == len(kept)


def test_gate_messages_do_not_shift_turn_numbers(ivyea_home):
    """门禁注回的 user 消息不是提问 —— 它不能把后面几轮的轮号顶偏。"""
    sessions = _sessions(ivyea_home)
    sid = sessions.new_id()
    sessions.append_turn(sid, "sys", [{"role": "user", "content": "做 A"}])
    sessions.append_turn(sid, "sys", [
        {"role": "assistant", "content": "草稿"},
        {"role": "user", "content": transcript.gate_text(transcript.CITATION_GATE, "补引用")},
        {"role": "assistant", "content": "终稿"},
    ])
    assert sessions.current_turn_index(sid) == 0
    sessions.append_turn(sid, "sys", [{"role": "user", "content": "第二问"}])
    assert sessions.current_turn_index(sid) == 1


def test_note_turn_time_ignores_unknown_session(ivyea_home):
    sessions = _sessions(ivyea_home)
    sessions.note_turn_time("no-such-session", 0, started_at=time.time())   # 不抛
    assert sessions.turn_times("no-such-session") == []


def test_turn_times_are_not_wiped_by_a_plain_save(ivyea_home):
    """`save()` 是整份覆盖语义，但它不知道时间账的存在 —— 不能顺手抹掉。"""
    sessions = _sessions(ivyea_home)
    sid = sessions.new_id()
    sessions.append_turn(sid, "sys", [{"role": "user", "content": "问"}])
    sessions.note_turn_time(sid, 0, started_at=5.0, ended_at=9.0, ms=4000)
    sessions.save(sid, sessions.load(sid)["messages"], model="m")
    assert sessions.turn_times(sid)[0]["ms"] == 4000
