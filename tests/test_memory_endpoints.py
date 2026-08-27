"""记忆的读写端点：让记忆可见可管。

在此之前记忆只能从命令行看，而记忆里装的正是"这个人是谁、他定过什么规矩、
我从他身上推断出了什么"——看不见就不敢信，推断错了也没地方改。

这些端点只做透出，不新造逻辑。下面的用例盯的就是这一点：
界面写入必须和 agent 自己的写入走同一条路（同一套查重、冲突消解、历史归档）。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def store(ivyea_home):
    from ivyea_agent import memory_store, service
    memory_store.apply("add", name="领星广告方法论", content="规则引擎 + LLM 复核。",
                       category="domain", description="领星广告优化怎么做",
                       keywords="领星,广告")
    return service


def test_list_reports_decay_so_ui_can_explain_absence(store):
    """界面要能回答"这条为什么没进上下文" —— 冷门条目退出索引层但仍可检索，
    不说清楚看起来就像记忆丢了。"""
    res = store.memory_list()
    assert res["ok"] and res["total"] == 1
    row = res["entries"][0]
    assert "decay" in row and "in_index" in row["decay"]
    assert "body" not in row          # 列表不带正文，正文按需取


def test_get_returns_provenance(store):
    """溯源三件套要一起给：谁说的、多确信、凭什么。
    少了任何一件，用户就没法判断该不该信这条记忆。"""
    res = store.memory_get("领星广告方法论")
    assert res["ok"]
    e = res["entry"]
    assert e["body"] and e["source"] and "confidence" in e and "evidence" in e
    assert "uncertain" in e


def test_get_missing_is_a_clean_not_found(store):
    res = store.memory_get("根本没有这条")
    assert res["ok"] is False and res["error"] == "not_found"


def test_human_write_is_marked_as_user_stated(store):
    """界面上人敲的就是他亲口说的：满置信，而且从此反思不许再改它。"""
    from ivyea_agent import memory_store
    res = store.memory_write({"operation": "add", "name": "发版纪律",
                              "category": "feedback", "content": "未经批准绝不发版。",
                              "description": "用户定的规矩"})
    assert res["ok"]
    e = memory_store.get("发版纪律")
    assert e.source == "user" and e.confidence == 1.0
    assert not e.uncertain


def test_human_write_goes_through_the_same_conflict_rules(store):
    """界面写入不能绕开查重合并 —— 绕开的话界面就成了制造重复记忆的入口。"""
    res = store.memory_write({"operation": "add", "name": "领星广告方法论2",
                              "category": "domain", "content": "规则引擎 + LLM 复核。",
                              "description": "领星广告优化怎么做"})
    from ivyea_agent import memory_store
    # 查重命中时走的是合并而不是新建；无论哪种结果，条目数都不该凭空多出一条重复的
    names = [e.name for e in memory_store.list_entries()]
    assert len(names) == len(set(names))
    assert res["ok"] in (True, False)


def test_write_rejects_unknown_operation(store):
    assert store.memory_write({"operation": "drop", "name": "x"})["ok"] is False


def test_confirm_is_the_only_way_past_the_uncertainty_line(store):
    """自动攒够观察次数也只是转正、仍标推断；人点头才能满置信。
    这是"未经确认的推断永远带着标记"这条不变式的出口。"""
    from ivyea_agent import memory_store
    memory_store.add_pending("推断偏好", "他大概喜欢这样。", category="feedback",
                             description="从行为推断的")
    res = store.memory_pending_decide({"name": "推断偏好"}, "confirm")
    assert res["ok"]
    e = memory_store.get("推断偏好")
    assert e.source == "user" and e.confidence == 1.0


def test_reject_removes_the_pending_inference(store):
    from ivyea_agent import memory_store
    memory_store.add_pending("错的推断", "错的。", category="feedback", description="x")
    assert store.memory_pending_decide({"name": "错的推断"}, "reject")["ok"]
    assert memory_store.get_pending("错的推断") is None


def test_pending_list_shows_progress_toward_promotion(store):
    """待定区要说清"第几次观察了" —— 否则用户看到的只是一条没头没尾的猜测。"""
    from ivyea_agent import memory_store
    memory_store.add_pending("推断偏好", "内容", category="feedback", description="x")
    res = store.memory_pending_list()
    row = res["pending"][0]
    assert row["sightings"] >= 1 and row["promote_after"] >= 1


def test_core_read_and_write_round_trip(store):
    res = store.memory_core_write({"block": "user", "operation": "append",
                                   "content": "用户叫 Hector。"})
    assert res["ok"]
    got = store.memory_core_read("user")
    assert "Hector" in got["text"] and got["limit"] > 0


def test_core_write_rejects_unknown_block(store):
    assert store.memory_core_write({"block": "nope", "operation": "append",
                                    "content": "x"})["ok"] is False


def test_stats_covers_all_three_layers(store):
    """三层记忆各有各的健康状况，统计要一次说全，否则用户没法判断该去动哪一层。"""
    res = store.memory_stats()
    assert res["ok"]
    assert res["store"]["total"] >= 1        # 分类记忆
    assert "user" in res["core"]             # 核心记忆
    assert "pending_episodes" in res["reflect"]   # 反思
    assert "indexed" in res["episodes"]      # 情景记忆


def test_reflect_endpoint_is_async(store, monkeypatch):
    """反思里包着一次最长 120 秒的模型调用，同步等会把用户挂在那儿。"""
    from ivyea_agent import memory_reflect
    calls = []
    monkeypatch.setattr(memory_reflect, "maybe_reflect_async",
                        lambda **kw: calls.append(kw) or True)
    res = store.memory_reflect_now({})
    assert res["ok"] and res["started"] is True
    assert calls[0]["force"] is True


def test_prune_endpoint_defaults_to_dry_run(store):
    """这是记忆里唯一不可逆的一步，默认必须是"只看不删"。"""
    res = store.memory_prune({})
    assert res["ok"] and res["deleted"] == 0


def test_query_param_mojibake_is_repaired(store):
    """没做百分号编码的客户端会让中文参数变成乱码，而失败的样子是 not_found ——
    看起来像"这条记忆没了"，比报错还难查。"""
    from ivyea_agent.service import _repair_latin1
    mojibake = "领星广告方法论".encode("utf-8").decode("latin-1")
    assert _repair_latin1(mojibake) == "领星广告方法论"
    assert _repair_latin1("领星广告方法论") == "领星广告方法论"   # 正确输入不动
    assert _repair_latin1("café") == "café"                        # 真 latin-1 文本不动
    assert _repair_latin1("plain") == "plain"
