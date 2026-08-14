"""关联图：把 [[links]] 从装饰变成真功能。

在此之前 links 字段只是写在 frontmatter 里好看，**没有任何代码读它**。
接上之后，召回一条记忆会把它显式链接到的相关记忆一并带出来——
"想起一件事会带出相关的事"。

两条刻意的限制：只走 1 跳、只走正向链接。放开会让一次召回把半个记忆库拖进上下文。
"""
from __future__ import annotations


def _mk(memory_store, name, desc, content, links=""):
    memory_store.apply("add", name=name, category="domain", description=desc,
                       content=content, links=links)
    return memory_store.get(name)


def test_parse_links_from_field_and_body(ivyea_home):
    """正文里的 [[xxx]] 也要认——模型写正文时很自然会顺手打，强制只写 frontmatter 容易漏。"""
    from ivyea_agent import memory_store
    e = _mk(memory_store, "甲", "描述甲", "正文里提到 [[乙]] 和 [[丙]]", links="[[丁]]")
    assert set(memory_store.entry_links(e)) == {"乙", "丙", "丁"}


def test_parse_links_dedupes(ivyea_home):
    from ivyea_agent import memory_store
    e = _mk(memory_store, "甲", "描述", "[[乙]] 又提了一次 [[乙]]", links="[[乙]]")
    assert memory_store.entry_links(e) == ["乙"]


def test_parse_links_empty(ivyea_home):
    from ivyea_agent import memory_store
    assert memory_store.parse_links("") == []
    assert memory_store.parse_links("没有链接的文本") == []


def test_expand_brings_linked_memory(ivyea_home):
    """核心功能：召回 A 时把 A 链接到的 B 带出来。"""
    from ivyea_agent import memory_store
    _mk(memory_store, "否词阈值", "否词标准", "≥15点击0单才否", links="[[保护词清单]]")
    _mk(memory_store, "保护词清单", "绝不否定的词", "品牌词、核心品类词")
    hits = [memory_store.get("否词阈值")]
    expanded = memory_store.expand_linked(hits)
    assert [e.name for e in expanded] == ["否词阈值", "保护词清单"]


def test_expand_stops_at_one_hop(ivyea_home):
    """A→B→C 只带出 B，不带 C。放开跳数会把半个记忆库拖进上下文。"""
    from ivyea_agent import memory_store
    _mk(memory_store, "甲", "描述甲", "内容甲", links="[[乙]]")
    _mk(memory_store, "乙", "描述乙", "内容乙", links="[[丙]]")
    _mk(memory_store, "丙", "描述丙", "内容丙")
    names = [e.name for e in memory_store.expand_linked([memory_store.get("甲")])]
    assert names == ["甲", "乙"]


def test_expand_respects_max_linked(ivyea_home):
    """一条 hub 记忆链接特别多时不能淹没结果。"""
    from ivyea_agent import memory_store
    for n in "乙丙丁戊己":
        _mk(memory_store, n, f"描述{n}", f"内容{n}")
    _mk(memory_store, "甲", "描述甲", "内容甲", links="".join(f"[[{n}]]" for n in "乙丙丁戊己"))
    out = memory_store.expand_linked([memory_store.get("甲")], max_linked=2)
    assert len(out) == 3          # 自己 + 2 条


def test_expand_skips_missing_targets(ivyea_home):
    """链接到不存在的记忆是常态（模型会先写链接后建记忆），不能因此报错。"""
    from ivyea_agent import memory_store
    _mk(memory_store, "甲", "描述甲", "内容甲", links="[[根本不存在的记忆]]")
    assert [e.name for e in memory_store.expand_linked([memory_store.get("甲")])] == ["甲"]


def test_expand_skips_expired_targets(ivyea_home):
    """已失效的记忆不该被联想带回上下文——那正是过期规则误导决策的路径。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="旧规则", category="domain", description="旧的",
                       content="过期内容", valid_until="2020-01-01")
    _mk(memory_store, "甲", "描述甲", "内容甲", links="[[旧规则]]")
    assert [e.name for e in memory_store.expand_linked([memory_store.get("甲")])] == ["甲"]


def test_expand_does_not_duplicate_existing_hits(ivyea_home):
    from ivyea_agent import memory_store
    _mk(memory_store, "甲", "描述甲", "内容甲", links="[[乙]]")
    _mk(memory_store, "乙", "描述乙", "内容乙", links="[[甲]]")
    hits = [memory_store.get("甲"), memory_store.get("乙")]
    assert len(memory_store.expand_linked(hits)) == 2


def test_expand_empty_input(ivyea_home):
    from ivyea_agent import memory_store
    assert memory_store.expand_linked([]) == []


def test_backlinks(ivyea_home):
    from ivyea_agent import memory_store
    _mk(memory_store, "甲", "描述甲", "内容甲", links="[[目标]]")
    _mk(memory_store, "乙", "描述乙", "内容乙", links="[[目标]]")
    _mk(memory_store, "目标", "被指向的", "内容")
    assert {e.name for e in memory_store.backlinks("目标")} == {"甲", "乙"}


def test_link_suggestions_on_add(ivyea_home):
    """推荐而不自动建链：自动建的链会把"用词相似"当成"内容相关"，攒几个月就是一张噪音图。"""
    from ivyea_agent import memory_store
    _mk(memory_store, "库存周转", "库存周转天数怎么看", "低于30天补货")
    res = memory_store.apply("add", name="断货预警", category="domain",
                             description="库存周转告警怎么设", content="低于20天报警")
    assert res["ok"]
    assert "[[库存周转]]" in res["message"]


def test_no_suggestion_when_links_given(ivyea_home):
    """已经写了链接就别再刷推荐。"""
    from ivyea_agent import memory_store
    _mk(memory_store, "库存周转", "库存周转天数", "低于30天补货")
    res = memory_store.apply("add", name="断货预警", category="domain",
                             description="库存周转告警", content="低于20天报警",
                             links="[[库存周转]]")
    assert "可能相关" not in res["message"]


def test_search_tool_brings_linked(ivyea_home):
    """端到端：memory_search 工具的输出里要出现被关联带出的那条。"""
    from ivyea_agent import agent_tools, memory_store
    _mk(memory_store, "否词阈值", "否词标准是什么", "≥15点击0单才否", links="[[保护词清单]]")
    _mk(memory_store, "保护词清单", "哪些词绝不否定", "品牌词、核心品类词")
    out = agent_tools.dispatch("memory_search", {"query": "否词标准"}, agent_tools.ToolContext())
    assert "否词阈值" in out
    assert "保护词清单" in out and "关联带出" in out


def test_search_tool_marks_uncertain(ivyea_home):
    """推断出来的记忆在工具输出里也要带标记，模型才不会当成用户原话执行。"""
    from ivyea_agent import agent_tools, memory_store
    memory_store.apply("add", name="推断项", category="feedback", description="独特关键词描述",
                       content="内容", source="reflection", confidence=0.5)
    out = agent_tools.dispatch("memory_search", {"query": "独特关键词描述"},
                               agent_tools.ToolContext())
    assert "⚠推断" in out
