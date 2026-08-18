"""核心记忆单测：agent 自编辑 USER.md / AGENTS.md。

核心记忆**每轮**都注入 system prompt，所以两件事必须测死：
1. 不能无限增长（会悄悄吃光上下文）；
2. replace 不能改错位置（高价值小文本，改错比没改代价大）。
"""
from __future__ import annotations



def test_append_creates_file_with_date(ivyea_home):
    from ivyea_agent import memory_core
    res = memory_core.edit("user", "append", "汇报一律用中文")
    assert res["ok"]
    text = memory_core.view("user")
    assert "汇报一律用中文" in text
    # 绝对日期：相对时间过几天就是错的，而核心记忆会被反复读到
    assert "- [20" in text


def test_append_rejects_empty(ivyea_home):
    from ivyea_agent import memory_core
    assert not memory_core.edit("user", "append", "   ")["ok"]


def test_unknown_block_rejected(ivyea_home):
    from ivyea_agent import memory_core
    res = memory_core.edit("nonexistent", "append", "x")
    assert not res["ok"] and "未知" in res["message"]


def test_unknown_operation_rejected(ivyea_home):
    from ivyea_agent import memory_core
    assert not memory_core.edit("user", "frobnicate", "x")["ok"]


def test_replace_requires_unique_match(ivyea_home):
    """old 命中多处必须拒绝，不能赌它改的是哪一处。"""
    from ivyea_agent import memory_core
    memory_core.edit("agents", "append", "目标 ACoS 25%")
    memory_core.edit("agents", "append", "目标 ACoS 25%")
    res = memory_core.edit("agents", "replace", "目标 ACoS 20%", old="目标 ACoS 25%")
    assert not res["ok"] and "不唯一" in res["message"]


def test_replace_missing_old_rejected(ivyea_home):
    from ivyea_agent import memory_core
    memory_core.edit("agents", "append", "保护词：品牌词")
    res = memory_core.edit("agents", "replace", "新内容", old="根本不存在的原文")
    assert not res["ok"]


def test_replace_applies(ivyea_home):
    from ivyea_agent import memory_core
    memory_core.edit("agents", "append", "目标 ACoS 25%")
    res = memory_core.edit("agents", "replace", "目标 ACoS 18%", old="目标 ACoS 25%")
    assert res["ok"]
    assert "18%" in memory_core.view("agents")
    assert "25%" not in memory_core.view("agents")


def test_remove_deletes_line(ivyea_home):
    from ivyea_agent import memory_core
    memory_core.edit("user", "append", "喜欢简短汇报")
    memory_core.edit("user", "append", "不要用表情符号")
    res = memory_core.edit("user", "remove", old="表情符号")
    assert res["ok"]
    text = memory_core.view("user")
    assert "喜欢简短汇报" in text and "表情符号" not in text


def test_remove_no_match_rejected(ivyea_home):
    from ivyea_agent import memory_core
    memory_core.edit("user", "append", "一条")
    assert not memory_core.edit("user", "remove", old="不存在")["ok"]


def test_size_cap_enforced(ivyea_home):
    """写满上限必须**拒绝并提示合并**，不能默默截断（截断会悄悄丢事实）。"""
    from ivyea_agent import memory_core
    big = "x" * (memory_core.MAX_BLOCK_CHARS - 100)
    assert memory_core.edit("user", "append", big)["ok"]
    res = memory_core.edit("user", "append", "y" * 500)
    assert not res["ok"] and "上限" in res["message"]
    # 被拒绝后原内容必须完好无损
    assert big in memory_core.view("user")


def test_crowded_warning(ivyea_home):
    from ivyea_agent import memory_core
    threshold = int(memory_core.MAX_BLOCK_CHARS * memory_core.CROWDED_RATIO)
    res = memory_core.edit("user", "append", "z" * (threshold + 10))
    assert res["ok"] and res["crowded"] and "接近上限" in res["message"]


def test_atomic_write_leaves_no_temp_files(ivyea_home):
    from ivyea_agent import memory_core
    memory_core.edit("user", "append", "一条要点")
    assert not list(ivyea_home.glob("*.tmp*"))


def test_status_shape(ivyea_home):
    from ivyea_agent import memory_core
    memory_core.edit("user", "append", "abc")
    st = memory_core.status()
    assert st["user"]["exists"] and st["user"]["file"] == "USER.md"
    assert st["agents"]["exists"] is False


def test_core_memory_feeds_load_instructions(ivyea_home):
    """消费方契约：写进核心记忆的内容必须真的被 load_instructions 注入，
    否则 agent 改了个寂寞。这是整条链路唯一真正重要的断言。"""
    from ivyea_agent import memory, memory_core
    memory_core.edit("user", "append", "用户是 Hector，做亚马逊运营")
    memory_core.edit("agents", "append", "品牌词永远不否")
    injected = memory.load_instructions()
    assert "用户是 Hector" in injected
    assert "品牌词永远不否" in injected


def test_tools_registered_and_dispatch(ivyea_home):
    """工具必须真的注册进 schema 和 dispatch，光有模块函数没用。"""
    from ivyea_agent import agent_tools
    names = {t["function"]["name"] for t in agent_tools.TOOL_SCHEMAS}
    assert {"core_memory_view", "core_memory_edit"} <= names
    ctx = agent_tools.ToolContext()
    out = agent_tools.dispatch("core_memory_edit",
                               {"block": "user", "operation": "append", "content": "测试写入"}, ctx)
    assert "已更新核心记忆" in out
    assert "测试写入" in agent_tools.dispatch("core_memory_view", {"block": "user"}, ctx)


def test_subagent_can_view_but_not_edit_core_memory(ivyea_home):
    """只读子 agent 不该改主人的长期画像——那是主线才有权做的决定。"""
    from ivyea_agent import agent_tools
    assert "core_memory_view" in agent_tools.READONLY_TOOLS
    assert "core_memory_edit" not in agent_tools.READONLY_TOOLS
