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


class _FakeClock:
    """可控的时钟。

    这条用例最初用真 sleep 卡阈值（模型端 sleep(0.04)×2、工具端 sleep(0.3)，断言
    llm_ms < 250），本机十次都是 80ms，**但在 GitHub 的 macOS runner 上抖到了 347ms**，
    main 上挂了个红叉。那个断言实际上量的是 runner 的调度抖动，不是"工具时间有没有
    被算进去"——而后者才是这里要钉的东西。

    换成假时钟：谁推进多少由用例说了算，llm_ms 变成一个精确值，跑在多慢的机器上都一样。
    """

    def __init__(self, real):
        self._real = real
        # 从 0 起算：大基数下浮点会吃掉精度（1000.04 - 1000.0 = 0.03999…），
        # 而生产代码是 int((t1-t0)*1000) 截断，于是 40ms 会变成 39ms。
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        # time.time / datetime 等照旧走真实实现 —— 只接管 monotonic 这一处。
        return getattr(self._real, name)


class _ModelThenToolProvider:
    """第一步想一会儿去调工具，第二步想一会儿给答案。每步模型窗口推进 50ms。"""

    def __init__(self, clock: _FakeClock):
        self.clock = clock
        self.calls = 0

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        self.clock.advance(0.05)
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

    clock = _FakeClock(time)
    # 只替换 agent_loop 命名空间里的 time，不动全局 time 模块 —— 否则同进程里
    # 别的东西（pytest 自己、后续用例）也会跟着看到一个假时钟。
    monkeypatch.setattr(agent_loop, "time", clock)
    # 工具执行推进 500ms：比两次模型窗口加起来还久。它一旦被算进 llm_ms，下面那条
    # 精确断言立刻就红。
    monkeypatch.setattr(agent_loop, "_dispatch_tool_calls",
                        lambda *a, **k: clock.advance(0.5))

    out = agent_loop.run_turn_stream(
        _ModelThenToolProvider(clock), ToolContext(), [{"role": "user", "content": "hi"}],
        max_steps=3, render=lambda _: None, narrate=lambda _: None)

    # 两步模型窗口各 50ms，一步不多一步不少；工具那 500ms 一点没混进来。
    assert out["usage"]["llm_ms"] == 100
    # 而这一轮"挂钟时间"是 600ms —— 差额 500ms 就是工具花掉的，这正是这个指标的用途。
    assert round(clock.now, 6) == 0.6


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
