"""历史会话详情：按轮分页 + 执行步骤落盘。

这两件事修的是同一个投诉："刷新之后再点开历史会话，自己发的一部分指令和整个执行
过程都不见了"。

根因不是偶发：详情此前和 live 回包共用一个投影，末尾 `rows[-30:]`。一次提问能产生
几十条消息（本机实测 2 次提问 → 62 条，其中 31 条是 tool），工具调用把名额吃光，
用户自己那句话被挤出窗口。本机最惨的一条会话：413 条消息、15 次提问，刷新后只剩 1 条。

所以这里钉的是**按轮**分页 —— 按条切多少条都会重蹈覆辙。
"""
from __future__ import annotations

import json

from ivyea_agent import service, sessions, transcript


def _turn(idx: int, with_tool: bool = True) -> list[dict]:
    """一轮：提问 →（可选）调一次工具 → 回答。"""
    rows: list[dict] = [{"role": "user", "content": f"第 {idx} 个问题"}]
    if with_tool:
        rows += [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": f"call-{idx}", "type": "function",
                             "function": {"name": "run_command", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"call-{idx}", "content": "x" * 5000},
        ]
    rows.append({"role": "assistant", "content": f"第 {idx} 个回答"})
    return rows


def _session(turns: int = 12) -> dict:
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(turns):
        msgs += _turn(i)
    steps = [{"type": "step", "id": f"call-{i}", "seq": i, "phase": "tool",
              "name": "run_command", "status": "ok", "ms": 12, "args": {"command": "ls"}}
             for i in range(turns)]
    return {"id": "s", "messages": msgs, "steps": steps,
            "skill_matches": [{"anchor": "call-3", "skills": [{"id": "k", "title": "技能"}]}]}


def test_every_question_is_reachable_by_paging():
    """**这条是主诉**：一页一页翻下去，12 个提问一个都不能少。"""
    data = _session(12)
    seen, before, pages = [], None, 0
    while True:
        page = service._public_session_detail(data, turns=5, before=before)
        seen = [m["content"] for m in page["messages"] if m["role"] == "user"] + seen
        pages += 1
        if not page["turns"]["has_more"]:
            break
        before = page["turns"]["from"]
    assert seen == [f"第 {i} 个问题" for i in range(12)]
    assert pages == 3                                   # 5 + 5 + 2


def test_first_page_is_the_latest_turns():
    page = service._public_session_detail(_session(12), turns=4)
    assert page["turns"] == {"total": 12, "from": 8, "to": 12, "has_more": True}
    assert [m["content"] for m in page["messages"] if m["role"] == "user"] == \
        [f"第 {i} 个问题" for i in (8, 9, 10, 11)]


def test_gate_messages_do_not_count_as_a_turn():
    """门禁注回的伪 user 消息不是一次提问 —— 算成一轮的话，分页就会切出空轮，
    而且"总共几轮"这个数会比用户记得的多出一截。"""
    msgs = [{"role": "user", "content": "真提问"},
            {"role": "assistant", "content": "草稿"},
            {"role": "user", "content": transcript.gate_text(transcript.VERIFY_GATE, "：补上自验证")},
            {"role": "assistant", "content": "终稿"}]
    page = service._public_session_detail({"id": "s", "messages": msgs})
    assert page["turns"]["total"] == 1
    users = [m["content"] for m in page["messages"] if m["role"] == "user"]
    assert users == ["真提问"]


def test_steps_come_back_with_the_turns_they_belong_to():
    """步骤靠 call_id 挂回轮次，不靠下标 —— 压缩过、导入过的会话都不会错位。"""
    page = service._public_session_detail(_session(12), turns=3)      # 第 9/10/11 轮
    assert [s["id"] for s in page["steps"]] == ["call-9", "call-10", "call-11"]
    # 技能锚在第 3 轮，不该混进这一页
    assert page["skill_matches"] == []
    early = service._public_session_detail(_session(12), turns=3, before=5)   # 第 2/3/4 轮
    assert [s["anchor"] for s in early["skill_matches"]] == ["call-3"]


def test_tool_output_is_trimmed_but_conversation_is_not():
    """工具返回界面从来不渲染（本机最大会话里它占 278KB），截断；
    对话正文一个字都不许动 —— 那正是用户要回看的东西。"""
    page = service._public_session_detail(_session(3), turns=99)
    tool_rows = [m for m in page["messages"] if m["role"] == "tool"]
    assert tool_rows and all(len(m["content"]) < 1200 for m in tool_rows)
    assert all(m["content"].endswith("（已截断）") for m in tool_rows)
    assert [m["content"] for m in page["messages"] if m["role"] == "user"] == \
        ["第 0 个问题", "第 1 个问题", "第 2 个问题"]


def test_call_ids_are_exposed_so_the_ui_can_join_steps():
    page = service._public_session_detail(_session(2), turns=99)
    assistant_calls = [m for m in page["messages"] if m.get("tool_calls")]
    assert [c["id"] for m in assistant_calls for c in m["tool_calls"]] == ["call-0", "call-1"]
    assert [c["name"] for m in assistant_calls for c in m["tool_calls"]] == ["run_command"] * 2
    assert [m["tool_call_id"] for m in page["messages"] if m["role"] == "tool"] == \
        ["call-0", "call-1"]


def test_sessions_without_steps_still_open():
    """改动之前落盘的会话没有 steps 字段 —— 打开时只是没有执行过程，不能炸。"""
    page = service._public_session_detail({"id": "s", "messages": _turn(0)})
    assert page["steps"] == [] and page["skill_matches"] == []
    assert page["turns"]["total"] == 1


def test_turnless_session_does_not_blow_up():
    assert service._public_session_detail({"id": "s", "messages": []})["turns"]["total"] == 0


# ── 落盘 ────────────────────────────────────────────────────────────────────

def test_steps_are_appended_not_overwritten(ivyea_home):
    """并发收尾时整份覆盖会让先写的那一轮消失 —— 消息早就是追加语义，步骤同理。"""
    sid = sessions.new_id()
    sessions.append_turn(sid, "sys", _turn(0), steps=[{"id": "call-0", "status": "ok"}])
    sessions.append_turn(sid, "sys", _turn(1), steps=[{"id": "call-1", "status": "ok"}])
    got = sessions.load(sid)
    assert [s["id"] for s in got["steps"]] == ["call-0", "call-1"]


def test_plain_save_keeps_the_steps(ivyea_home):
    """`save()` 是整份覆盖语义（CLI 每轮就这么写），它不认识步骤，
    但不该顺手把已经落盘的执行过程抹掉。"""
    sid = sessions.new_id()
    sessions.append_turn(sid, "sys", _turn(0), steps=[{"id": "call-0", "status": "ok"}])
    sessions.save(sid, sessions.load(sid)["messages"] + _turn(1))
    assert [s["id"] for s in sessions.load(sid)["steps"]] == ["call-0"]


def test_step_history_is_capped(ivyea_home):
    """长会话不能让步骤数组无限长下去。"""
    sid = sessions.new_id()
    sessions.append_turn(sid, "sys", _turn(0),
                         steps=[{"id": f"c{i}"} for i in range(sessions._STEPS_MAX + 50)])
    kept = sessions.load(sid)["steps"]
    assert len(kept) == sessions._STEPS_MAX
    assert kept[-1]["id"] == f"c{sessions._STEPS_MAX + 49}"      # 留的是最近的


def test_persisted_session_is_still_valid_json_for_the_model(ivyea_home):
    """步骤必须存在 messages **之外**：messages 里的 dict 会原样回灌给模型 API，
    多一个自定义键就有被 provider 拒的风险。"""
    sid = sessions.new_id()
    sessions.append_turn(sid, "sys", _turn(0), steps=[{"id": "call-0"}])
    raw = json.loads((sessions.path_for(sid)).read_text(encoding="utf-8"))
    assert "steps" in raw
    allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
    for msg in raw["messages"]:
        assert set(msg) <= allowed, f"消息里混进了自定义键：{set(msg) - allowed}"


def test_a_real_turn_persists_its_execution_steps(ivyea_home):
    """端到端：跑一轮带工具的对话，步骤要真的落盘、并能按轮取回来。

    这条才是"刷新之后执行过程还在"的真凭据 —— 上面那些用例喂的是手搓数据。
    """
    class _ToolThenDone:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "final", "content": "", "usage": {},
                       "tool_calls": [{"id": "c1", "name": "list_dir", "arguments": {"path": "."}}]}
                return
            yield {"type": "text", "text": "看完了"}
            yield {"type": "final", "content": "看完了", "tool_calls": [], "usage": {}}

    events: list[tuple[str, dict]] = []
    out = service.chat_stream(
        {"message": "看一下目录", "max_steps": 3, "inject_retrieval": False},
        lambda ev, data: events.append((ev, data)),
        provider=_ToolThenDone(),
    )
    sid = out["session_id"]

    stored = sessions.load(sid)
    assert [s["name"] for s in stored["steps"]] == ["list_dir"]
    # 只留最终态：一次调用发了 running 和 ok 两条事件，落盘不该留两份
    assert stored["steps"][0]["status"] == "ok"
    assert sum(1 for e, _ in events if e == "step") >= 2

    detail = service.chat_session_detail(sid)["session"]
    assert [s["name"] for s in detail["steps"]] == ["list_dir"]
    assert [m["content"] for m in detail["messages"] if m["role"] == "user"] == ["看一下目录"]
