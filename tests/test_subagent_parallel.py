"""并行子 agent + run_turn_stream 受限工具子集。"""
from __future__ import annotations

import threading


def test_run_turn_stream_passes_custom_tools(ivyea_home):
    """回归：run_turn_stream 把传入的受限工具子集喂给 provider（此前硬编码全量）。"""
    from ivyea_agent import agent_loop, agent_tools
    seen = {}

    class _P:
        def stream_chat(self, messages, tools=None, **kw):
            seen["tools"] = tools
            yield {"type": "final", "content": "ok", "tool_calls": [], "usage": {}}

    subset = [t for t in agent_tools.TOOL_SCHEMAS if t["function"]["name"] == "read_file"]
    ctx = agent_tools.ToolContext()
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}]
    agent_loop.run_turn_stream(_P(), ctx, msgs, render=lambda s: None,
                               narrate=lambda s: None, tools=subset)
    assert seen["tools"] is subset


def test_dispatch_subagent_is_parallel_safe(ivyea_home):
    from ivyea_agent.agent_tools import PARALLEL_SAFE, READONLY_TOOLS
    assert "dispatch_subagent" in PARALLEL_SAFE
    assert "dispatch_subagent" not in READONLY_TOOLS   # 子 agent 不能递归再派子 agent


def test_two_subagents_run_concurrently(ivyea_home):
    """真并行验证：两个子 agent 的 chat() 用 Barrier(2) 互相等——只有并发才能双双过闸。"""
    from ivyea_agent import agent_loop, agent_tools

    barrier = threading.Barrier(2, timeout=10)

    class _SubProvider:
        """主脑 stream_chat 第一步派 2 个子 agent；子 agent 走 chat()（非流式）。"""
        def __init__(self):
            self.step = 0

        def stream_chat(self, messages, tools=None, **kw):
            self.step += 1
            if self.step == 1:
                yield {"type": "final", "content": "", "usage": {},
                       "tool_calls": [
                           {"id": "s1", "name": "dispatch_subagent", "arguments": {"task": "查A"}},
                           {"id": "s2", "name": "dispatch_subagent", "arguments": {"task": "查B"}}]}
            else:
                yield {"type": "final", "content": "汇总完成", "tool_calls": [], "usage": {}}

        def chat(self, messages, tools=None, **kw):
            barrier.wait()          # 串行执行会在这死等 10s 后 BrokenBarrierError
            task = messages[-1]["content"]
            return {"content": f"{task} 的结论", "tool_calls": []}

    prov = _SubProvider()
    ctx = agent_tools.ToolContext(provider=prov)
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "并行调研"}]
    out = agent_loop.run_turn_stream(prov, ctx, msgs, render=lambda s: None, narrate=lambda s: None)
    assert out["text"] == "汇总完成"
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    # 结果按原顺序回灌且各自对应
    assert tool_msgs[0]["tool_call_id"] == "s1" and "查A" in tool_msgs[0]["content"]
    assert tool_msgs[1]["tool_call_id"] == "s2" and "查B" in tool_msgs[1]["content"]


def test_subagent_max_steps_cap_configurable(ivyea_home):
    """max_steps 上限从写死 20 → 配置键 subagent_max_steps_cap（默认 40）。"""
    from ivyea_agent import agent_tools, config

    captured = {}

    class _EchoProvider:
        def chat(self, messages, tools=None, **kw):
            return {"content": "done", "tool_calls": []}

    import ivyea_agent.agent_loop as agent_loop
    orig = agent_loop.run_turn

    def _spy(provider, ctx, messages, max_steps=None, **kw):
        captured["max_steps"] = max_steps
        return orig(provider, ctx, messages, max_steps=max_steps, **kw)

    try:
        agent_loop.run_turn = _spy
        ctx = agent_tools.ToolContext(provider=_EchoProvider())
        agent_tools.dispatch("dispatch_subagent", {"task": "x", "max_steps": 100}, ctx)
        assert captured["max_steps"] == 40            # 默认上限
        config.set_setting("subagent_max_steps_cap", 25)
        agent_tools.dispatch("dispatch_subagent", {"task": "x", "max_steps": 100}, ctx)
        assert captured["max_steps"] == 25            # 配置生效
    finally:
        agent_loop.run_turn = orig
