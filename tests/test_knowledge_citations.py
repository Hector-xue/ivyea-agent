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


def test_nonstream_citation_gate_retries_then_appends_used_source():
    from ivyea_agent import agent_loop
    from ivyea_agent.agent_tools import ToolContext

    class Provider:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return {"content": "注册资料要保持一致。", "tool_calls": []}
            assert "知识引用门禁" in messages[-1]["content"]
            return {"content": "官方注册流程要求核对企业与身份资料。[K1]", "tool_calls": []}

    provider = Provider()
    ctx = ToolContext(knowledge_citations=[_citation()], knowledge_retrieval_expected=True, knowledge_risk="high")
    messages = [{"role": "user", "content": "注册验证失败怎么办"}]
    text = agent_loop.run_turn(provider, ctx, messages, max_steps=3, narrate=lambda _: None)

    assert provider.calls == 2
    assert "[K1]" in text
    assert "引用知识：" in text
    assert "https://sell.amazon.com/sell/registration-guide" in text
    assert messages[-1]["content"] == text


def test_stream_citation_gate_does_not_render_uncited_draft():
    from ivyea_agent import agent_loop
    from ivyea_agent.agent_tools import ToolContext

    class Provider:
        def __init__(self):
            self.calls = 0

        def stream_chat(self, messages, tools=None):
            self.calls += 1
            content = "未引用草稿" if self.calls == 1 else "已按官方证据回答。[K1]"
            yield {"type": "text", "text": content}
            yield {"type": "final", "content": content, "tool_calls": [], "usage": {}}

    rendered = []
    ctx = ToolContext(knowledge_citations=[_citation()], knowledge_retrieval_expected=True, knowledge_risk="high")
    result = agent_loop.run_turn_stream(
        Provider(), ctx, [{"role": "user", "content": "注册问题"}], max_steps=3,
        render=rendered.append, narrate=lambda _: None,
    )
    output = "".join(rendered)
    assert "未引用草稿" not in output
    assert "已按官方证据回答。[K1]" in output
    assert "引用知识：" in result["text"]


def test_knowledge_tool_registers_citations_on_context():
    from ivyea_agent.agent_tools import ToolContext, dispatch

    ctx = ToolContext()
    text = dispatch("knowledge_search", {"query": "上架报错 90220", "limit": 2}, ctx)
    assert "[K1]" in text
    assert ctx.knowledge_retrieval_expected is True
    assert ctx.knowledge_risk == "high"
    assert ctx.knowledge_citations[0]["url"].startswith("https://developer-docs.amazon.com/")


def test_repeated_knowledge_searches_keep_stable_unique_keys():
    from ivyea_agent.agent_tools import ToolContext, dispatch

    ctx = ToolContext()
    first = dispatch("knowledge_search", {"query": "上架报错 90220", "limit": 1}, ctx)
    first_id = ctx.knowledge_citations[0]["id"]
    second = dispatch("knowledge_search", {"query": "卖家注册身份验证", "limit": 2}, ctx)
    keys = [row["key"] for row in ctx.knowledge_citations]
    assert first_id == "seller_central.listings_items_error_diagnostics"
    assert first.startswith("检索决策") and "[K1]" in first
    assert len(keys) == len(set(keys))
    assert keys[0] == "K1"
    assert "[K2]" in second
