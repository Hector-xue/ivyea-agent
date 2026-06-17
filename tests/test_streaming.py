"""M1+ 内核：SSE 解析、成本核算、计划模式、流式循环。"""
from __future__ import annotations

from ivyea_agent.providers.openai_compat import parse_sse


def test_parse_sse_text_and_tool_calls():
    lines = [
        'data: {"choices":[{"delta":{"content":"你好"}}]}',
        'data: {"choices":[{"delta":{"content":"世界"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"run_patrol","arguments":"{\\"sid\\""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":1876}"}}]}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}',
        "data: [DONE]",
    ]
    events = list(parse_sse(lines))
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert texts == ["你好", "世界"]
    final = events[-1]
    assert final["type"] == "final"
    assert final["content"] == "你好世界"
    assert final["tool_calls"] == [{"id": "call_1", "name": "run_patrol", "arguments": {"sid": 1876}}]
    assert final["usage"]["prompt_tokens"] == 10


def test_parse_sse_handles_bytes_and_blank():
    lines = [b'data: {"choices":[{"delta":{"content":"hi"}}]}', "", b"data: [DONE]"]
    events = list(parse_sse(lines))
    assert events[0]["text"] == "hi" and events[-1]["type"] == "final"


def test_pricing_estimate(ivyea_home):
    from ivyea_agent import pricing
    # deepseek-chat: input 2, cached 0.5, output 8（¥/1M）
    cost = pricing.estimate("deepseek-chat", {"prompt_tokens": 1_000_000, "completion_tokens": 0})
    assert abs(cost - 2.0) < 1e-9
    cost2 = pricing.estimate("deepseek-chat", {"prompt_tokens": 1_000_000,
                                               "prompt_cache_hit_tokens": 1_000_000, "completion_tokens": 0})
    assert abs(cost2 - 0.5) < 1e-9  # 全缓存命中


def test_usage_meter(ivyea_home):
    from ivyea_agent import pricing
    m = pricing.UsageMeter()
    m.add("deepseek-chat", {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
    assert m.turns == 1 and m.prompt == 1_000_000 and m.completion == 1_000_000
    assert abs(m.cost - 10.0) < 1e-9  # 2 + 8


def test_plan_mode_blocks_execute(ivyea_home):
    from ivyea_agent import agent_tools
    ctx = agent_tools.ToolContext(plan_mode=True)
    out = agent_tools.dispatch("execute_actions", {}, ctx)
    assert "计划模式" in out


class _FakeProvider:
    """模拟流式：先吐两段文本，再 final（无工具调用）。"""
    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        yield {"type": "text", "text": "分析"}
        yield {"type": "text", "text": "完成"}
        yield {"type": "final", "content": "分析完成", "tool_calls": [],
               "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


def test_run_turn_stream_no_tools(ivyea_home):
    from ivyea_agent import agent_loop, agent_tools
    ctx = agent_tools.ToolContext()
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "看下广告"}]
    chunks = []
    out = agent_loop.run_turn_stream(_FakeProvider(), ctx, msgs, render=chunks.append,
                                     narrate=lambda s: None, model="deepseek-chat")
    assert out["text"] == "分析完成"
    assert "".join(chunks).startswith("分析完成")  # 逐字渲染过
    assert out["usage"]["prompt_tokens"] == 100
    assert msgs[-1] == {"role": "assistant", "content": "分析完成"}


class _ToolThenDoneProvider:
    """第一步要求调用工具，第二步收尾。"""
    def __init__(self):
        self.calls = 0
    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "final", "content": "", "usage": {"prompt_tokens": 50, "completion_tokens": 10},
                   "tool_calls": [{"id": "c1", "name": "recall", "arguments": {"query": "放量"}}]}
        else:
            yield {"type": "text", "text": "好了"}
            yield {"type": "final", "content": "好了", "tool_calls": [],
                   "usage": {"prompt_tokens": 60, "completion_tokens": 8}}


def test_run_turn_stream_with_tool(ivyea_home):
    from ivyea_agent import agent_loop, agent_tools
    ctx = agent_tools.ToolContext()
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "回忆放量"}]
    out = agent_loop.run_turn_stream(_ToolThenDoneProvider(), ctx, msgs,
                                     render=lambda s: None, narrate=lambda s: None, model="deepseek-chat")
    assert out["text"] == "好了"
    # 两步用量累加
    assert out["usage"]["prompt_tokens"] == 110 and out["usage"]["completion_tokens"] == 18
    # 含一次 assistant(tool_calls) + 一次 tool 结果
    assert any(m.get("role") == "tool" for m in msgs)
