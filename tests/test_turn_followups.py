"""任务跑到一半还能补一句话：收件箱 → 步边界注入 → 逐轮时间账。

钉住的契约（每条都对应一种真实失败）：
① 收件箱只在**步边界**被读走，绝不出现在 assistant(tool_calls) 与 tool 结果之间
   —— 那个位置插一条 user 消息会被 provider 拒掉整轮；
② 模型都要收工了才收到的追加指令**也算数**（收尾前再看一眼收件箱），否则用户
   在"快跑完了"那一刻补的话总是要等下一轮；
③ 没被消费的条目不许无声消失，调用方要能端出来另作安排；
④ 追加指令是**真实的用户提问**，不能被 strip_injected 当成注回消息摘掉 ——
   摘掉的话历史会话里"我中途说过什么"就整段没了。
"""
from __future__ import annotations

import threading

from ivyea_agent import agent_loop, transcript, turn_inbox


def teardown_function():
    turn_inbox.reset_for_tests()


# ── 收件箱 ──────────────────────────────────────────────────────────────────

def test_submit_and_drain_round_trip():
    assert turn_inbox.submit("s1", "顺便把标题也改了")["ok"] is True
    assert turn_inbox.submit("s1", "别动价格")["ok"] is True
    items = turn_inbox.drain("s1")
    assert [i["text"] for i in items] == ["顺便把标题也改了", "别动价格"]
    assert turn_inbox.drain("s1") == []          # 排空即消费，不重复投递


def test_submit_rejects_blank_and_overlong():
    assert turn_inbox.submit("s1", "   ")["ok"] is False
    assert turn_inbox.submit("", "x")["ok"] is False
    assert turn_inbox.submit("s1", "x" * (turn_inbox.MAX_TEXT + 1))["error"] == "text_too_long"


def test_inbox_full_is_explicit_not_silent():
    for i in range(turn_inbox.MAX_PENDING):
        assert turn_inbox.submit("s1", f"第{i}条")["ok"] is True
    out = turn_inbox.submit("s1", "再来一条")
    assert out["ok"] is False and out["error"] == "inbox_full"
    assert len(turn_inbox.pending("s1")) == turn_inbox.MAX_PENDING


def test_sessions_are_isolated():
    turn_inbox.submit("a", "给 a 的")
    turn_inbox.submit("b", "给 b 的")
    assert [i["text"] for i in turn_inbox.drain("a")] == ["给 a 的"]
    assert [i["text"] for i in turn_inbox.drain("b")] == ["给 b 的"]


def test_concurrent_submits_all_land():
    def worker(n):
        turn_inbox.submit("s1", f"第 {n} 条")
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(turn_inbox.MAX_PENDING)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(turn_inbox.drain("s1")) == turn_inbox.MAX_PENDING


# ── 步边界注入 ──────────────────────────────────────────────────────────────

def test_drain_injections_appends_user_message_and_resets_guard():
    from ivyea_agent import loop_guard
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "做 A"}]
    guard = loop_guard.LoopGuard()
    guard.steps_since_progress = 5
    seen: list = []
    got = agent_loop._drain_injections(
        messages, lambda: [{"id": "1", "text": "改成做 B"}], seen.append, lambda _s: None, guard)

    assert len(got) == 1
    assert messages[-1] == {"role": "user", "content": f"{agent_loop.INJECT_PREFIX} 改成做 B"}
    assert seen == [{"id": "1", "text": "改成做 B"}]
    # 局面变了：之前攒的"卡住"判定必须作废，否则用户刚说完话就被告知卡住了
    assert guard.steps_since_progress == 0


def test_drain_injections_is_noop_without_channel():
    messages = [{"role": "user", "content": "做 A"}]
    assert agent_loop._drain_injections(messages, None, None, lambda _s: None, None) == []
    assert len(messages) == 1


def test_drain_injections_survives_a_broken_channel():
    """取追加指令时抛异常，绝不能把正在跑的轮次带崩。"""
    def boom():
        raise RuntimeError("inbox down")
    messages = [{"role": "user", "content": "做 A"}]
    assert agent_loop._drain_injections(messages, boom, None, lambda _s: None, None) == []
    assert len(messages) == 1


def test_injected_instruction_is_not_stripped_from_history():
    """追加指令是用户真说过的话，历史里必须留着（对比门禁注回消息会被摘掉）。"""
    text = f"{agent_loop.INJECT_PREFIX} 顺便把标题也改了"
    assert transcript.is_injected_user_message(text) is False
    kept = transcript.strip_injected([
        {"role": "user", "content": "做 A"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": text},
    ])
    assert [m["content"] for m in kept][-1] == text
    # 而且它自成一轮 —— 界面上要能看到"我中途说了这句"和它之后的动作
    assert len(transcript.turn_slices(kept)) == 2


# ── 跑到一半插进去：整轮视角 ────────────────────────────────────────────────

class _TwoStepProvider:
    """第一步调一个工具，第二步给最终答案。"""

    def __init__(self):
        self.calls = 0
        self.seen: list[list] = []

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls == 1:
            yield {"type": "final", "content": "先看一眼", "usage": {},
                   "tool_calls": [{"id": "c1", "name": "list_dir",
                                   "arguments": {"path": "."}}]}
        else:
            yield {"type": "text", "text": "做完了"}
            yield {"type": "final", "content": "做完了", "tool_calls": [], "usage": {}}


def _ctx(tmp_path=None):
    from ivyea_agent.agent_tools import ToolContext
    return ToolContext(session_id="s1", workspace=str(tmp_path or "."))


def test_followup_lands_in_the_running_turn(tmp_path):
    """核心场景：轮次跑着的时候投一条，模型下一步就看得见。"""
    turn_inbox.submit("s1", "顺便把标题也改了")
    provider = _TwoStepProvider()
    events: list = []
    agent_loop.run_turn_stream(
        provider, _ctx(tmp_path), [{"role": "user", "content": "做 A"}], max_steps=4,
        render=lambda _t: None, narrate=lambda _s: None,
        inject_check=lambda: turn_inbox.drain("s1"),
        on_inject=events.append,
    )
    # 第二次问模型时，追加指令已经在上下文里了
    second = provider.seen[1]
    assert any(m["role"] == "user" and "顺便把标题也改了" in str(m["content"]) for m in second)
    assert [e["text"] for e in events] == ["顺便把标题也改了"]


def test_followup_never_splits_a_tool_call_from_its_result():
    """user 消息插在 assistant(tool_calls) 与 tool 结果之间会被 provider 拒掉整轮。"""
    provider = _TwoStepProvider()

    def always_one():
        return [{"id": "x", "text": "补一句"}]

    agent_loop.run_turn_stream(
        provider, _ctx(), [{"role": "user", "content": "做 A"}], max_steps=3,
        render=lambda _t: None, narrate=lambda _s: None, inject_check=always_one)

    for snapshot in provider.seen:
        for i, msg in enumerate(snapshot):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # 紧跟着的必须是它的 tool 结果，不能是插进来的 user 消息
                assert snapshot[i + 1]["role"] == "tool", snapshot[i + 1]


class _OneShotProvider:
    """一步就给最终答案。第一次生成到一半时，用户补了一句（真实时序）。"""

    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        text = "第一份答案" if self.calls == 1 else "补充做完了"
        yield {"type": "text", "text": text}
        if self.calls == 1:
            turn_inbox.submit("s1", "等等，再改一处")     # 用户在模型说话时打了字
        yield {"type": "final", "content": text, "tool_calls": [], "usage": {}}


def test_followup_arriving_at_the_last_moment_still_runs_this_turn():
    """用户最常补话的时刻就是"快跑完了"那一下——不能让它一律等下一轮。"""
    provider = _OneShotProvider()
    resets: list[str] = []
    out = agent_loop.run_turn_stream(
        provider, _ctx(), [{"role": "user", "content": "做 A"}], max_steps=4,
        render=lambda _t: None, narrate=lambda _s: None, on_answer_reset=resets.append,
        inject_check=lambda: turn_inbox.drain("s1"))

    assert provider.calls == 2                 # 收工前又被叫回来干了一轮
    assert out["text"] == "补充做完了"
    # 这一段正文没有作废（用户只是追加要求）→ 前端只断段、不清屏
    assert resets == ["user_inject"]
