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


class _AlwaysToolProvider:
    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        yield {"type": "final", "content": "", "usage": {"prompt_tokens": 1, "completion_tokens": 1},
               "tool_calls": [{"id": f"c{len(messages)}", "name": "recall", "arguments": {"query": "x"}}]}


def test_run_turn_stream_uses_configured_tool_step_limit(ivyea_home):
    from ivyea_agent import agent_loop, agent_tools, config
    config.set_setting("chat_max_tool_steps", 2)
    ctx = agent_tools.ToolContext()
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "一直查"}]
    notes = []
    out = agent_loop.run_turn_stream(_AlwaysToolProvider(), ctx, msgs,
                                     render=lambda s: None, narrate=notes.append)
    assert "工具调用步数上限 2" in out["text"]
    assert "不要重复已经成功的工具调用" in out["text"]
    assert out["usage"]["prompt_tokens"] == 2
    assert msgs[-1]["role"] == "assistant"
    assert "最后一个未完成的小步骤" in msgs[-1]["content"]
    assert any("工具预算剩余" in n for n in notes)


def test_run_turn_stream_records_limit_trace(ivyea_home):
    from ivyea_agent import agent_loop, agent_tools, traces
    ctx = agent_tools.ToolContext(session_id="sid-1")
    ctx.turn_id = "turn-1"
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "一直查"}]
    out = agent_loop.run_turn_stream(_AlwaysToolProvider(), ctx, msgs,
                                     max_steps=1, render=lambda s: None, narrate=lambda s: None)
    assert "工具调用步数上限 1" in out["text"]
    recent = traces.recent(limit=5, session_id="sid-1")
    assert any(r["event"] == "turn_limit" and r["name"] == "tool_steps" for r in recent)


def test_run_turn_stream_cancel_check_interrupts_before_final_append(ivyea_home):
    from ivyea_agent import agent_loop, agent_tools
    ctx = agent_tools.ToolContext()
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "取消测试"}]
    checks = {"n": 0}

    def cancel_check():
        checks["n"] += 1
        return checks["n"] >= 2

    try:
        agent_loop.run_turn_stream(_FakeProvider(), ctx, msgs, render=lambda s: None,
                                   narrate=lambda s: None, cancel_check=cancel_check)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("expected KeyboardInterrupt")

    assert msgs == [{"role": "system", "content": "x"}, {"role": "user", "content": "取消测试"}]


class _AlwaysToolChatProvider:
    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        return {"content": "", "tool_calls": [{"id": f"c{len(messages)}", "name": "recall", "arguments": {"query": "x"}}]}


def test_run_turn_non_stream_limit_adds_resume_context(ivyea_home):
    from ivyea_agent import agent_loop, agent_tools
    ctx = agent_tools.ToolContext()
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "一直查"}]
    notes = []
    out = agent_loop.run_turn(_AlwaysToolChatProvider(), ctx, msgs, max_steps=1, narrate=notes.append)
    assert "工具调用步数上限 1" in out
    assert msgs[-1]["role"] == "assistant"
    assert "不要重复已经成功的工具调用" in msgs[-1]["content"]
    assert any("工具预算剩余" in n for n in notes)


def test_run_turn_stream_limit_updates_bound_task(ivyea_home, tmp_path, monkeypatch):
    from ivyea_agent import agent_loop, agent_tools, task_runner
    monkeypatch.setattr(task_runner, "TASK_DIR", tmp_path / "tasks")
    task = task_runner.create("Long agent task", steps=["inspect", "finish"])
    task_runner.start_next(task["id"])
    ctx = agent_tools.ToolContext(task_id=task["id"])
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "一直查"}]
    agent_loop.run_turn_stream(_AlwaysToolProvider(), ctx, msgs,
                               max_steps=1, render=lambda s: None, narrate=lambda s: None)
    saved = task_runner.load(task["id"])
    assert saved["status"] == "blocked"
    assert saved["steps"][0]["status"] == "blocked"
    assert saved["resume"]["reason"] == "tool_step_limit"
    assert saved["resume"]["state"]["max_steps"] == 1
    assert saved["resume"]["state"]["tool_calls"] == 1
    assert "不要重复上一轮已经成功的工具调用" in saved["resume"]["prompt"]
    assert any(ev["kind"] == "interrupted" for ev in saved["events"])
