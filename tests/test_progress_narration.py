"""连着只干活不吭声时，向模型要一句阶段汇报。

起因是一条真实会话：模型连跑 30 步（20 次抓网页、6 分钟）**一个字都没说**，
用户看到的只有一列滚动的工具名。存档里那一轮 25 条 assistant 消息 content 全是空 ——
界面画不出没发生的话，所以这句话只能在模型这一侧要。
"""
from __future__ import annotations


class _SilentToolProvider:
    """每一步都调工具、每一步都不说话（复现那一轮的形状）。"""

    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        self.calls += 1
        yield {"type": "final", "content": "", "usage": {},
               "tool_calls": [{"id": f"c{self.calls}", "name": "recall",
                               "arguments": {"query": f"查第 {self.calls} 项"}}]}


class _TalkingToolProvider:
    """每一步都先说一句再调工具 —— 这种模型不该被打扰。"""

    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        self.calls += 1
        yield {"type": "final", "content": f"先查第 {self.calls} 项", "usage": {},
               "tool_calls": [{"id": f"c{self.calls}", "name": "recall",
                               "arguments": {"query": f"查第 {self.calls} 项"}}]}


def _nudges(messages: list) -> list[str]:
    from ivyea_agent import agent_loop
    return [m["content"] for m in messages
            if m.get("role") == "tool" and agent_loop._NARRATION_MARKER in str(m.get("content") or "")]


def _run(provider, ctx, steps=6):
    from ivyea_agent import agent_loop
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "介绍一下这个岗位"}]
    agent_loop.run_turn_stream(provider, ctx, msgs, max_steps=steps,
                               render=lambda _s: None, narrate=lambda _s: None)
    return msgs


def test_silent_streak_asks_for_a_status_line(ivyea_home):
    from ivyea_agent import agent_loop, agent_tools

    msgs = _run(_SilentToolProvider(), agent_tools.ToolContext())
    got = _nudges(msgs)
    # 第 4 步（_SILENT_STEPS_BEFORE_NUDGE）上要一次；要过之后计数清零，
    # 剩下两步不够再攒够一轮 —— 否则每一步都挂一句，就成了刷屏。
    assert len(got) == 1, got
    assert "先用一两句话" in got[0]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert agent_loop._NARRATION_MARKER in tool_msgs[agent_loop._SILENT_STEPS_BEFORE_NUDGE - 1]["content"]


def test_short_run_is_left_alone(ivyea_home):
    """三步以内的普通对话不催 —— 短任务最好的汇报就是直接把答案给出来。"""
    from ivyea_agent import agent_tools

    assert _nudges(_run(_SilentToolProvider(), agent_tools.ToolContext(), steps=3)) == []


def test_model_that_already_narrates_is_not_nudged(ivyea_home):
    from ivyea_agent import agent_tools

    assert _nudges(_run(_TalkingToolProvider(), agent_tools.ToolContext())) == []


def test_progress_lifecycle_turns_are_not_double_prompted():
    """progress_required 的轮次已有一整套强制汇报（todo_write/progress_update），不叠第二套。

    直接打这个判据而不是跑一整轮：progress_required 是 task_scope 每轮按指令重算的
    （见 run_turn_stream 开头的 prepare_messages），在轮外手工设上去会被它覆盖掉，
    那样的测试只是在测 task_scope 的分类结果，不是在测这里的取舍。
    """
    from ivyea_agent import agent_loop

    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "结果"}]
    ctx = type("C", (), {"progress_required": True})()
    assert agent_loop._nudge_progress_narration(ctx, msgs, 99) is False
    assert msgs[0]["content"] == "结果"


def test_nudge_never_lands_twice_on_the_same_results(ivyea_home):
    from ivyea_agent import agent_loop

    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "结果"}]
    ctx = type("C", (), {})()
    assert agent_loop._nudge_progress_narration(ctx, msgs, 9) is True
    assert agent_loop._nudge_progress_narration(ctx, msgs, 9) is False
    assert msgs[0]["content"].count(agent_loop._NARRATION_MARKER) == 1
