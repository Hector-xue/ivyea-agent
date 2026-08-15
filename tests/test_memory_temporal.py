"""时效双时间轴 + 作用域。

运营场景里几乎所有事实都有有效期：目标 ACoS 旺季一个值淡季一个值、某个词 6 月否了
8 月又放开。没有事实时间轴就只能二选一——覆盖（丢历史，答不了"什么时候改的"）
或并存（留下互相矛盾的两条让 agent 瞎猜）。

向后兼容是硬要求：老文件没有这些字段，**绝不能因此被判失效而凭空消失**。
"""
from __future__ import annotations

import time

TODAY = time.strftime("%Y-%m-%d")


# ── 向后兼容 ────────────────────────────────────────────────────────────────
def test_legacy_entry_without_fields_is_valid(ivyea_home):
    """没有时效字段的老记忆 = 一直有效、全局适用。这条挂了就是升级即丢数据。"""
    from ivyea_agent import memory_store
    d = ivyea_home / "memory" / "domain"
    d.mkdir(parents=True)
    (d / "老记忆.md").write_text("---\nname: 老记忆\ncategory: domain\n---\n\n正文\n",
                                 encoding="utf-8")
    entries = memory_store.list_entries()
    assert [e.name for e in entries] == ["老记忆"]
    assert entries[0].is_valid_on() and entries[0].matches_scope("store:任意")


def test_unparseable_date_does_not_hide_entry(ivyea_home):
    """用户手写了 '下个月' 这种解析不出来的日期，宁可当长期有效，也不能让记忆消失。"""
    from ivyea_agent import memory_store
    d = ivyea_home / "memory" / "domain"
    d.mkdir(parents=True)
    (d / "怪日期.md").write_text(
        "---\nname: 怪日期\ncategory: domain\nvalid_until: 下个月\n---\n\n正文\n", encoding="utf-8")
    assert [e.name for e in memory_store.list_entries()] == ["怪日期"]


def test_date_normalization(ivyea_home):
    from ivyea_agent import memory_store
    assert memory_store._norm_day("2026/8/1") == "2026-08-01"
    assert memory_store._norm_day("2026-8-1") == "2026-08-01"
    assert memory_store._norm_day("2026-08-01") == "2026-08-01"


# ── 有效期过滤 ──────────────────────────────────────────────────────────────
def test_expired_entry_excluded_from_search(ivyea_home):
    """已失效的记忆出现在检索结果里只会误导 agent。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="旺季阈值", category="domain",
                       description="旺季目标ACoS", content="旺季目标 ACoS 35%",
                       valid_until="2020-01-01")
    assert memory_store.search("旺季阈值") == []
    assert memory_store.index_digest() == ""


def test_future_entry_not_yet_active(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="双十一规则", category="domain",
                       description="大促期规则", content="大促期放宽到 40%",
                       valid_from="2099-01-01")
    assert memory_store.search("双十一规则") == []


def test_expired_still_reachable_explicitly(ivyea_home):
    """按名字点名要，即便失效也该拿得到——否则 update 过期记忆会报找不到，只能新建，又碎片化。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="旧规则", category="domain", description="d",
                       content="旧的", valid_until="2020-01-01")
    assert memory_store.get("旧规则") is not None
    assert memory_store.search("旧规则", include_expired=True)


def test_update_expired_entry_works(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="阈值", category="domain", description="d",
                       content="旧值", valid_until="2020-01-01")
    res = memory_store.apply("update", name="阈值", content="新值", valid_until="")
    assert res["ok"]
    assert memory_store.search("阈值")            # 更新后重新有效


# ── 历史归档 ────────────────────────────────────────────────────────────────
def test_update_archives_old_body(ivyea_home):
    """"这个阈值以前是多少、什么时候改的"本身就是要回答的问题，覆盖式更新会永久抹掉它。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="目标ACoS", category="domain",
                       description="目标 ACoS", content="目标 ACoS 25%")
    res = memory_store.apply("update", name="目标ACoS", content="目标 ACoS 18%")
    assert res["ok"] and res["archived"]
    hist = memory_store.history("目标ACoS")
    assert len(hist) == 1
    assert "25%" in hist[0].body
    assert hist[0].valid_until == TODAY
    assert "18%" in memory_store.get("目标ACoS").body


def test_metadata_only_update_does_not_archive(ivyea_home):
    """只改描述不该在历史里留一条内容相同的版本，否则真正的事实变更淹没在噪音里。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="打法", category="domain", description="旧描述", content="正文")
    memory_store.apply("update", name="打法", content="正文", description="新描述")
    assert memory_store.history("打法") == []


def test_multiple_updates_stack_history(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="阈值", category="domain", description="d", content="v1")
    memory_store.apply("update", name="阈值", content="v2")
    memory_store.apply("update", name="阈值", content="v3")
    bodies = [h.body.strip() for h in memory_store.history("阈值")]
    assert set(bodies) == {"v1", "v2"}
    assert memory_store.get("阈值").body.strip() == "v3"


def test_history_not_returned_by_search(ivyea_home):
    """历史版本绝不能混进正常检索——那等于把矛盾的新旧两个值一起喂给 agent。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="阈值", category="domain",
                       description="目标阈值", content="目标 ACoS 25%")
    memory_store.apply("update", name="阈值", content="目标 ACoS 18%")
    hits = memory_store.search("目标 ACoS")
    assert len(hits) == 1 and "18%" in hits[0]["body"]


def test_valid_from_advances_on_content_change(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="阈值", category="domain", description="d", content="v1")
    memory_store.apply("update", name="阈值", content="v2")
    assert memory_store.get("阈值").valid_from == TODAY


def test_supersede_can_be_disabled(ivyea_home):
    """改错别字之类不想留历史时可以关掉。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="阈值", category="domain", description="d", content="v1")
    memory_store.apply("update", name="阈值", content="v2", supersede=False)
    assert memory_store.history("阈值") == []


# ── 作用域 ──────────────────────────────────────────────────────────────────
def test_scope_isolates_stores(ivyea_home):
    """多店铺时"目标 ACoS 25%"属于哪个账号必须分得清，否则规则会串到别的店。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="美国站打法", category="domain", description="美国站阈值",
                       content="ACoS 25%", scope="store:US")
    memory_store.apply("add", name="日本站打法", category="domain", description="日本站阈值",
                       content="ACoS 40%", scope="store:JP")
    us = [h["name"] for h in memory_store.search("阈值", scope="store:US")]
    assert us == ["美国站打法"]


def test_global_entry_matches_every_scope(ivyea_home):
    """没标作用域的是全局规则，任何店铺都适用。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="通用红线", category="domain",
                       description="通用红线", content="品牌词永远不否")
    assert memory_store.search("红线", scope="store:随便哪个")


def test_query_without_scope_sees_everything(ivyea_home):
    """用户没说是哪个店时不过滤——宁可多给，不能少给。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="美国站打法", category="domain", description="阈值",
                       content="x", scope="store:US")
    assert memory_store.search("阈值")


def test_duplicate_check_is_scope_aware(ivyea_home):
    """不同店铺的同名打法本来就该各记各的，查重不该跨作用域拦截。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="US出价", category="domain",
                       description="出价策略与阈值", content="降 bid 不超过 15%", scope="store:US")
    res = memory_store.apply("add", name="JP出价", category="domain",
                             description="出价策略与阈值", content="降 bid 不超过 15%",
                             scope="store:JP")
    assert res["ok"]


def test_expired_entry_does_not_block_new_fact(ivyea_home):
    """拿一条已失效的旧记忆去拦截新事实，等于永远记不下变更后的新值。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="旧阈值", category="domain",
                       description="目标 ACoS 阈值设定", content="目标 ACoS 25%",
                       valid_until="2020-01-01")
    res = memory_store.apply("add", name="新阈值", category="domain",
                             description="目标 ACoS 阈值设定", content="目标 ACoS 25%")
    assert res["ok"]


# ── 索引层 ──────────────────────────────────────────────────────────────────
def test_index_line_marks_scope_and_expiry(ivyea_home):
    from ivyea_agent import memory_store
    memory_store.apply("add", name="旺季规则", category="domain", description="旺季放宽",
                       content="x", scope="store:US", valid_until="2099-12-31")
    line = memory_store.index_digest()
    assert "store:US" in line and "至2099-12-31" in line


def test_index_line_stays_clean_without_dates(ivyea_home):
    """无限期的记忆不该每行挂个尾巴，索引层本来就要省字。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="常规打法", category="domain", description="描述", content="x")
    assert memory_store.index_digest().endswith("[domain/常规打法] 描述")


def test_tool_passes_temporal_fields(ivyea_home):
    from ivyea_agent import agent_tools, memory_store
    agent_tools.dispatch("memory_write", {
        "operation": "add", "name": "促销期规则", "category": "domain",
        "description": "促销期放宽", "content": "放宽到 40%",
        "scope": "store:US", "valid_until": "2099-01-01"}, agent_tools.ToolContext())
    e = memory_store.get("促销期规则")
    assert e.scope == "store:US" and e.valid_until == "2099-01-01"
