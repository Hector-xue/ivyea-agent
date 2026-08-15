"""统一回忆的集成测试：一次 recall 要能同时看到三层记忆和外部资源指针。

外加两条回归护栏：
- 消费方契约（retrieval/retrieval_index 依赖 search 的返回键）不能被改坏；
- 知识卡正文**不能**经 recall 泄漏——那会绕过引证键登记。
"""
from __future__ import annotations


def test_recall_surfaces_curated_memory_first(ivyea_home):
    from ivyea_agent import agent_tools, memory, memory_store
    memory_store.apply("add", name="宽泛词打法", category="domain",
                       description="对宽泛批发类词一贯保守", content="连续三个月否掉宽泛词。")
    memory.remember("6月否了一批宽泛词：cheap phone case")
    out = agent_tools.dispatch("recall", {"query": "宽泛词"}, agent_tools.ToolContext())
    assert "【分类记忆】" in out
    assert "[domain/宽泛词打法]" in out
    # 分类记忆（提炼过的结论）必须排在原始片段前面
    assert out.index("【分类记忆】") < out.index("【历史记录】")


def test_recall_includes_episodic(ivyea_home):
    from ivyea_agent import agent_tools, memory
    memory.remember("这个 ASIN 的库存只剩 12 天")
    out = agent_tools.dispatch("recall", {"query": "库存天数"}, agent_tools.ToolContext())
    assert "库存只剩 12 天" in out


def test_recall_empty(ivyea_home):
    from ivyea_agent import agent_tools
    out = agent_tools.dispatch("recall", {"query": "完全不存在的东西"}, agent_tools.ToolContext())
    assert "没有相关记录" in out


def test_recall_does_not_leak_knowledge_body(ivyea_home):
    """知识卡正文必须经 knowledge_search 走引证登记；recall 只给指针。
    否则模型会拿着没登记的 [K?] 键去标注结论，引证契约当场作废。"""
    from ivyea_agent import agent_tools, knowledge
    cards = knowledge.search("广告", limit=1)
    if not cards:
        return                       # 该环境没有内置知识卡，跳过
    out = agent_tools.dispatch("recall", {"query": "广告"}, agent_tools.ToolContext())
    if "【相关知识卡】" in out:
        assert "knowledge_search" in out          # 明确指路
        body = knowledge._read_body(cards[0])
        assert body[:200] not in out              # 正文没被吐出来


def test_recall_survives_missing_knowledge_base(ivyea_home, monkeypatch):
    """知识库出问题不该让回忆整个失败——记忆是更基础的能力。"""
    from ivyea_agent import agent_tools, knowledge, memory
    monkeypatch.setattr(knowledge, "search", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    memory.remember("一条普通记忆")
    out = agent_tools.dispatch("recall", {"query": "普通记忆"}, agent_tools.ToolContext())
    assert "一条普通记忆" in out


def test_search_shape_contract_for_retrieval_module(ivyea_home):
    """retrieval.py / retrieval_index.py 按 text/rowid/asin/ts 消费。改坏这个会静默打挂
    统一检索和 IvyeaOps 的嵌入索引，而它们没有自己的守卫测试。"""
    from ivyea_agent import memory
    memory.remember("契约验证")
    row = memory.search("契约验证")[0]
    for key in ("text", "rowid", "asin", "ts"):
        assert key in row
    assert memory.index_rows(limit=5)


def test_retrieval_module_still_works_end_to_end(ivyea_home):
    from ivyea_agent import memory, retrieval
    memory.remember("统一检索的回归验证")
    res = retrieval.search("统一检索", limit=5)
    assert res.get("hits") is not None
