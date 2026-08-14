"""分类记忆单测：一事一文件 + 索引层 + 冲突消解。

重点测的是**防碎片化**：同一件事分裂成多条是这类系统最常见的退化方式，
一旦碎了，检索精度和"更新某某记忆"这种交互都会跟着废掉。
"""
from __future__ import annotations


def test_add_creates_file_with_frontmatter(ivyea_home):
    from ivyea_agent import memory_store
    res = memory_store.apply("add", name="领星广告方法论", content="用确定性规则引擎+LLM复核。",
                             category="domain", description="领星广告优化怎么做",
                             keywords="领星,广告,规则引擎")
    assert res["ok"]
    e = memory_store.get("领星广告方法论")
    assert e is not None
    assert e.category == "domain"
    assert e.description == "领星广告优化怎么做"
    assert "规则引擎" in e.body
    assert e.created and e.updated


def test_chinese_name_preserved_in_filename(ivyea_home):
    """名字必须能被人叫出来——"更新领星那条记忆"这种交互全靠它。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="领星广告方法论", content="x", category="domain")
    assert (ivyea_home / "memory" / "domain" / "领星广告方法论.md").exists()


def test_unsafe_filename_chars_stripped(ivyea_home):
    from ivyea_agent import memory_store
    res = memory_store.apply("add", name="广告/优化:方案?", content="x", category="domain")
    assert res["ok"]
    assert (ivyea_home / "memory" / "domain" / "广告优化方案.md").exists()


def test_add_rejects_duplicate_name(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="否词阈值", content="≥15点击0单", category="domain")
    res = memory_store.apply("add", name="否词阈值", content="别的内容", category="domain")
    assert not res["ok"] and "update" in res["message"]


def test_add_detects_similar_and_pushes_update(ivyea_home):
    """防碎片化的核心：讲同一件事但换了名字，必须被拦下并指名已有那条。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="广告优化打法", category="domain",
                       description="广告优化的规则引擎打法",
                       content="广告优化用确定性规则引擎，数据不够不动手，四杠杆全做。")
    res = memory_store.apply("add", name="ACoS调优思路", category="domain",
                             description="广告优化的规则引擎打法",
                             content="广告优化用确定性规则引擎，数据不够不动手，四杠杆全做。")
    assert not res["ok"]
    assert res["similar_to"] == "广告优化打法"
    assert "update" in res["message"]


def test_add_allows_genuinely_different_entry(ivyea_home):
    """查重不能太激进——不相干的两条必须都能建。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="否词阈值", content="≥15点击0单才否", category="domain")
    res = memory_store.apply("add", name="飞书机器人部署", content="用 relay 服务转发消息到 CLI",
                             category="reference")
    assert res["ok"]


def test_update_overwrites_and_keeps_created(ivyea_home):
    """更新一条记忆不该抹掉它第一次被记住的时间。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="否词阈值", content="≥10点击0单", category="domain")
    created = memory_store.get("否词阈值").created
    res = memory_store.apply("update", name="否词阈值", content="≥15点击0单（改保守了）")
    assert res["ok"]
    e = memory_store.get("否词阈值")
    assert "15" in e.body and "10" not in e.body
    assert e.created == created


def test_update_missing_entry_does_not_silently_create(ivyea_home):
    """静默新建正是碎片化的来源，必须报错让调用方明确选择。"""
    from ivyea_agent import memory_store
    res = memory_store.apply("update", name="根本不存在", content="x")
    assert not res["ok"] and "add" in res["message"]


def test_delete_removes_file(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="过时结论", content="x", category="project")
    assert memory_store.apply("delete", name="过时结论")["ok"]
    assert memory_store.get("过时结论") is None


def test_delete_missing_reports_clearly(ivyea_home):
    from ivyea_agent import memory_store
    assert not memory_store.apply("delete", name="没有这条")["ok"]


def test_noop_is_a_valid_decision(ivyea_home):
    from ivyea_agent import memory_store
    res = memory_store.apply("noop")
    assert res["ok"] and res["operation"] == "noop"


def test_add_requires_valid_category(ivyea_home):
    from ivyea_agent import memory_store
    res = memory_store.apply("add", name="某条", content="x", category="随便发明的分类")
    assert not res["ok"] and "category" in res["message"]


def test_search_ranks_header_above_body(ivyea_home):
    """名字/描述的权重高于正文——标题是人提炼过的，更能代表这条记忆讲什么。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="库存周转", category="domain",
                       description="库存周转天数怎么看", content="无关正文")
    memory_store.apply("add", name="别的主题", category="domain",
                       description="别的描述", content="正文里顺便提了一句库存周转")
    hits = memory_store.search("库存周转")
    assert hits[0]["name"] == "库存周转"


def test_search_chinese_paraphrase(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="广告花费控制", category="domain",
                       description="ACoS 超标时怎么降广告花费",
                       content="超标就降 bid，单步不超过 15%。")
    assert memory_store.search("广告花钱太多怎么办")


def test_search_empty_query(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="某条", content="x", category="domain")
    assert memory_store.search("") == []


def test_index_digest_is_one_line_per_entry(ivyea_home):
    """索引层是省 token 的关键：每条一行，不含正文。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="条目甲", category="domain", description="描述甲",
                       content="非常长的正文" * 200)
    digest = memory_store.index_digest()
    assert "[domain/条目甲] 描述甲" in digest
    assert "非常长的正文非常长的正文" not in digest   # 正文绝不能进索引层
    assert len(digest) < 300


def test_index_digest_respects_cap(ivyea_home):
    """直接落盘造数据，绕开 apply 的查重——这里要测的是 index_digest 的截断，
    不该被"内容太像被拦下"这个无关行为干扰（那是下一个用例的事）。"""
    from ivyea_agent import memory_store
    d = ivyea_home / "memory" / "domain"
    d.mkdir(parents=True)
    for i in range(200):
        (d / f"条目{i}.md").write_text(
            f"---\nname: 条目{i}\ndescription: 描述{i}\ncategory: domain\nupdated: 2026-08-{i % 28 + 1:02d}\n---\n\n正文\n",
            encoding="utf-8")
    digest = memory_store.index_digest()
    assert len(digest) <= memory_store.MAX_INDEX_CHARS + 200   # 留一点分类标题/提示的余量
    assert "未列出" in digest                                   # 截断了要说出来


def test_near_identical_entries_are_blocked(ivyea_home):
    """措辞高度雷同的两条会被判为同一件事而拦下。这是刻意的保守取舍：
    宁可让模型确认一次，也不要放任记忆碎片化。被拦下不丢数据——
    提示里会指名已有那条，模型可以改 update 或换个更具体的名字。"""
    from ivyea_agent import memory_store
    assert memory_store.apply("add", name="条目甲", category="domain",
                              description="这是第1条记忆的描述文字", content="正文")["ok"]
    res = memory_store.apply("add", name="条目乙", category="domain",
                             description="这是第2条记忆的描述文字", content="正文")
    assert not res["ok"] and res["similar_to"] == "条目甲"


def test_index_digest_empty_when_no_entries(ivyea_home):
    from ivyea_agent import memory_store
    assert memory_store.index_digest() == ""


def test_broken_frontmatter_does_not_crash(ivyea_home):
    """用户手改文件写坏格式，代价应该是元数据缺失，不是整个记忆系统挂掉。"""
    from ivyea_agent import memory_store
    d = ivyea_home / "memory" / "domain"
    d.mkdir(parents=True)
    (d / "手改坏了.md").write_text("--- 这不是合法 frontmatter\n随便写的正文", encoding="utf-8")
    entries = memory_store.list_entries()
    assert any(e.name == "手改坏了" for e in entries)
    assert memory_store.index_digest()


def test_digest_flows_into_load_memory_digest(ivyea_home):
    """消费方契约：分类记忆索引必须真的被注入 system prompt，否则等于没做。"""
    from ivyea_agent import memory, memory_store
    memory_store.apply("add", name="注入验证", category="project",
                       description="这条要出现在系统提示里", content="正文")
    digest = memory.load_memory_digest()
    assert "分类记忆索引" in digest
    assert "[project/注入验证]" in digest


def test_tools_registered_and_dispatch(ivyea_home):
    from ivyea_agent import agent_tools
    names = {t["function"]["name"] for t in agent_tools.TOOL_SCHEMAS}
    assert {"memory_write", "memory_search", "memory_read"} <= names
    ctx = agent_tools.ToolContext()
    out = agent_tools.dispatch("memory_write", {
        "operation": "add", "name": "工具链路验证", "category": "project",
        "description": "验证工具能落盘", "content": "正文内容"}, ctx)
    assert "已新建记忆" in out
    assert "正文内容" in agent_tools.dispatch("memory_read", {"name": "工具链路验证"}, ctx)
    assert "工具链路验证" in agent_tools.dispatch("memory_search", {"query": "工具链路验证"}, ctx)


def test_subagent_cannot_write_memory(ivyea_home):
    from ivyea_agent import agent_tools
    assert {"memory_search", "memory_read"} <= agent_tools.READONLY_TOOLS
    assert "memory_write" not in agent_tools.READONLY_TOOLS


def test_atomic_write_leaves_no_temp_files(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="原子写", content="x", category="domain")
    assert not list((ivyea_home / "memory" / "domain").glob("*.tmp*"))
