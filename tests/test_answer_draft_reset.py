"""正文"草稿作废"边界（on_answer_reset）。

一轮里模型会把正文吐好几遍：工具前的开场白、门禁打回后的整篇重写。终端是一条
向下的日志，叠着看没问题；网页把 token 顺序拼进同一个气泡，用户看到的就是同一
张表连出三遍。run_turn_stream 在**新一稿的第一个字**上通知调用方作废上一稿。

CLI 不传这个回调 —— 所以这里也要钉死"不传时渲染序列一个字都不变"。
"""
from __future__ import annotations


def _citation():
    return {
        "key": "K1",
        "id": "seller_registration.registration_and_identity_verification",
        "title": "Seller registration and identity verification baseline",
        "url": "https://sell.amazon.com/sell/registration-guide",
        "authority_tier": "primary",
        "freshness": "current",
    }


class _GateProvider:
    """第一稿不带 [K#]（会被引用门禁打回），第二稿合规。"""

    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        content = ("库存周转要盯着补货节奏。" if self.calls == 1
                   else "官方口径下补货节奏按 IPI 走。[K1]")
        # 分片吐，和真实 provider 一样：reset 必须发在这一稿的第一片上
        for piece in (content[:6], content[6:]):
            yield {"type": "text", "text": piece}
        yield {"type": "final", "content": content, "tool_calls": [], "usage": {}}


def _web_ctx():
    """网页/serve 的形状：defer 关掉，边生成边流式。"""
    from ivyea_agent.agent_tools import ToolContext
    return ToolContext(knowledge_citations=[_citation()],
                       knowledge_retrieval_expected=True, knowledge_risk="high")


def test_gate_rewrite_reports_answer_reset_once():
    from ivyea_agent import agent_loop

    rendered: list[str] = []
    resets: list[str] = []
    provider = _GateProvider()
    out = agent_loop.run_turn_stream(
        provider, _web_ctx(), [{"role": "user", "content": "补货怎么排"}], max_steps=3,
        render=rendered.append, narrate=lambda _: None,
        defer_citation_text=False, on_answer_reset=resets.append,
    )

    assert provider.calls == 2                      # 确实被门禁打回重写了一遍
    assert resets == ["gate:citation"]              # 且只在第二稿开头作废一次

    # 前端按 reset 切段后，留在气泡里的只有最后一稿 —— 一份，不是两份。
    segments = "".join(rendered).split("库存周转要盯着补货节奏。")
    assert len(segments) == 2                       # 第一稿在流里出现过 1 次
    kept = "".join(rendered).split("官方口径下补货节奏按 IPI 走。")[-1]
    assert "库存周转" not in kept                    # 作废点之后不再有旧稿内容
    assert "[K1]" in out["text"]


def test_tool_preamble_is_superseded_by_the_answer(ivyea_home):
    from ivyea_agent import agent_loop
    from ivyea_agent.agent_tools import ToolContext

    class Provider:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "text", "text": "我先查一下历史结论。"}
                yield {"type": "final", "content": "我先查一下历史结论。", "usage": {},
                       "tool_calls": [{"id": "c1", "name": "recall", "arguments": {"query": "放量"}}]}
                return
            yield {"type": "text", "text": "结论：先放量再看转化。"}
            yield {"type": "final", "content": "结论：先放量再看转化。", "tool_calls": [], "usage": {}}

    resets: list[str] = []
    agent_loop.run_turn_stream(
        Provider(), ToolContext(), [{"role": "user", "content": "要不要放量"}], max_steps=4,
        render=lambda _: None, narrate=lambda _: None,
        defer_citation_text=False, on_answer_reset=resets.append,
    )
    assert resets == ["tool_call"]                  # 开场白让位给正式答案


def test_render_sequence_unchanged_without_callback():
    """CLI 路径零回归：不传 on_answer_reset 时逐字与改动前一致。"""
    from ivyea_agent import agent_loop

    def run(**extra):
        rendered: list[str] = []
        agent_loop.run_turn_stream(
            _GateProvider(), _web_ctx(), [{"role": "user", "content": "补货怎么排"}],
            max_steps=3, render=rendered.append, narrate=lambda _: None,
            defer_citation_text=False, **extra)
        return rendered

    assert run() == run(on_answer_reset=lambda _: None)


def test_reset_callback_failure_never_breaks_the_turn():
    """通知失败（前端断开、序列化炸了）不许打断正在跑的轮次。"""
    from ivyea_agent import agent_loop

    def boom(_reason):
        raise RuntimeError("client gone")

    out = agent_loop.run_turn_stream(
        _GateProvider(), _web_ctx(), [{"role": "user", "content": "补货怎么排"}],
        max_steps=3, render=lambda _: None, narrate=lambda _: None,
        defer_citation_text=False, on_answer_reset=boom)
    assert "[K1]" in out["text"]


def test_service_stream_emits_answer_reset_frame(ivyea_home):
    """serve 侧：这条边界要真的以 `answer_reset` 事件发到 SSE 上（前端按事件名分流）。

    走真实检索（inject_retrieval 默认开）—— 注意 `inject_retrieval: False` 会**清空**
    ctx.knowledge_citations，那样引用门禁根本不会触发，这条用例也就测了个寂寞。
    """
    from ivyea_agent import service

    events: list[tuple[str, dict]] = []
    result = service.chat_stream(
        {"message": "新卖家注册身份验证失败怎么办", "max_steps": 3, "persist": False},
        lambda event, data: events.append((event, data)),
        provider=_GateProvider(),
    )

    assert result["ok"] is True
    resets = [d for e, d in events if e == "answer_reset"]
    assert len(resets) == 1
    assert resets[0]["reason"] == "gate:citation"
    assert resets[0]["session_id"]                      # 前端要用它对上是哪一轮
    # 顺序也要对：作废发生在两段正文之间，不能跑到 final 后面去
    names = [e for e, _ in events]
    assert names.index("answer_reset") < names.index("final")
    assert names.index("token") < names.index("answer_reset")


def test_deferred_path_also_reports_reset():
    """defer 模式（CLI 默认）下门禁重写同样要给出边界 —— 只是 CLI 不接。"""
    from ivyea_agent import agent_loop

    resets: list[str] = []
    rendered: list[str] = []
    agent_loop.run_turn_stream(
        _GateProvider(), _web_ctx(), [{"role": "user", "content": "补货怎么排"}],
        max_steps=3, render=rendered.append, narrate=lambda _: None,
        defer_citation_text=True, on_answer_reset=resets.append)
    # defer 把未过门禁的草稿整段吞掉了，所以根本没有"上一稿"可作废
    assert resets == []
    assert "库存周转" not in "".join(rendered)
