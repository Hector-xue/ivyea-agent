"""思考流转发（serve → 网页）与 LLM 纯耗时（usage.llm_ms）。

两件事的动机是同一个：一轮跑了 20 分钟，调用方此前只拿得到一个总时长，说不出
这 20 分钟是模型在想、还是工具在跑，也说不出模型到底在想什么。

思考流**默认不发**：客户端的事件分发把未知事件当成"老 agent 的自由文本叙述"渲染，
默认开就等于让装着旧前端的用户一升级 agent 满屏思考碎片，且他关不掉。
所以这里既钉"要了就得给"，也钉"没要就一个字都不许发"。
"""
from __future__ import annotations

import time


class _ReasoningProvider:
    """先吐思考、再吐正文 —— deepseek-reasoner / claude / codex 的真实形状。"""

    def stream_chat(self, messages, tools=None):
        yield {"type": "reasoning", "text": "先看看库存周转"}
        yield {"type": "text", "text": "按 IPI 排补货。"}
        yield {"type": "final", "content": "按 IPI 排补货。", "tool_calls": [], "usage": {}}


class _SlowModelThenToolProvider:
    """第一步慢慢想完去调工具，第二步慢慢想完给答案。每步模型窗口 ~40ms。"""

    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        time.sleep(0.04)
        if self.calls == 1:
            yield {"type": "final", "content": "", "usage": {},
                   "tool_calls": [{"id": "c1", "name": "recall", "arguments": {"query": "库存"}}]}
            return
        yield {"type": "text", "text": "好了"}
        yield {"type": "final", "content": "好了", "tool_calls": [], "usage": {}}


def test_llm_ms_counts_the_model_window_only(monkeypatch):
    """llm_ms 必须把工具时间排除在外 —— 否则它和总时长是同一个数，等于什么都没说。"""
    from ivyea_agent import agent_loop
    from ivyea_agent.agent_tools import ToolContext

    def _slow_tools(*args, **kwargs):
        time.sleep(0.3)                      # 工具执行：比两次模型窗口加起来还久

    monkeypatch.setattr(agent_loop, "_dispatch_tool_calls", _slow_tools)

    started = time.monotonic()
    out = agent_loop.run_turn_stream(
        _SlowModelThenToolProvider(), ToolContext(), [{"role": "user", "content": "hi"}],
        max_steps=3, render=lambda _: None, narrate=lambda _: None)
    wall_ms = (time.monotonic() - started) * 1000

    llm_ms = out["usage"]["llm_ms"]
    assert llm_ms >= 60                      # 两步模型窗口 ~80ms，留足抖动余量
    assert llm_ms < 250                      # 300ms 的工具时间没被算进来
    assert wall_ms > 300                     # 而这一轮真实耗时确实超过了工具那 300ms


def test_llm_ms_is_always_present():
    """没有工具的轮次也要有这个字段 —— 调用方不该为"有时有有时没有"写分支。"""
    from ivyea_agent import agent_loop
    from ivyea_agent.agent_tools import ToolContext

    out = agent_loop.run_turn_stream(
        _ReasoningProvider(), ToolContext(), [{"role": "user", "content": "hi"}],
        render=lambda _: None, narrate=lambda _: None)
    assert "llm_ms" in out["usage"] and out["usage"]["llm_ms"] >= 0


def test_serve_keeps_quiet_about_thinking_unless_asked(ivyea_home):
    """**最要紧的一条**：没显式要，SSE 上不许出现 reasoning 事件。

    也不许改道跑到别的事件里去 —— 老前端把未知事件当叙述渲染，混进 event 一样刷屏。
    """
    from ivyea_agent import service

    events: list[tuple[str, dict]] = []
    result = service.chat_stream(
        {"message": "补货怎么排", "max_steps": 2, "persist": False, "inject_retrieval": False},
        lambda event, data: events.append((event, data)),
        provider=_ReasoningProvider(),
    )

    assert result["ok"] is True
    assert not [e for e, _ in events if e == "reasoning"]
    assert "先看看库存周转" not in "".join(str(d) for _, d in events)


def test_serve_streams_thinking_when_asked(ivyea_home):
    from ivyea_agent import service

    events: list[tuple[str, dict]] = []
    result = service.chat_stream(
        {"message": "补货怎么排", "max_steps": 2, "persist": False,
         "inject_retrieval": False, "stream_reasoning": True},
        lambda event, data: events.append((event, data)),
        provider=_ReasoningProvider(),
    )

    assert result["ok"] is True
    thinking = [d["text"] for e, d in events if e == "reasoning"]
    assert thinking == ["先看看库存周转"]
    # 思考不许混进正文：气泡里只能有回答
    assert "先看看库存周转" not in result["text"]
    names = [e for e, _ in events if e in ("reasoning", "token")]
    assert names[0] == "reasoning"                   # 想在前、说在后


def test_thinking_is_redacted_like_everything_else(ivyea_home):
    """模型思考里复述了密钥同样要打码 —— 它和 narrate/token 走的是同一条链路。"""
    from ivyea_agent import service

    class _LeakyProvider:
        def stream_chat(self, messages, tools=None):
            yield {"type": "reasoning", "text": "用 sk-abcdefghijklmnopqrstuvwxyz 这个键"}
            yield {"type": "text", "text": "好"}
            yield {"type": "final", "content": "好", "tool_calls": [], "usage": {}}

    events: list[tuple[str, dict]] = []
    service.chat_stream(
        {"message": "hi", "max_steps": 2, "persist": False,
         "inject_retrieval": False, "stream_reasoning": True},
        lambda event, data: events.append((event, data)),
        provider=_LeakyProvider(),
    )

    thinking = "".join(d["text"] for e, d in events if e == "reasoning")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in thinking
    assert "REDACTED" in thinking
