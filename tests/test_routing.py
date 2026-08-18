"""路线判定：闲聊要判得准，判不准要往安全的那边倒（work）。"""

import pytest

from ivyea_agent import routing


CHAT = [
    "你好", "您好", "hi", "hello", "在吗", "谢谢", "辛苦了", "好的", "收到",
    "你是谁", "你能做什么", "1+1等于几？", "今天几号", "讲个笑话", "早上好",
]

# 这些必须留在常规路线上：它们要么要查真实数据，要么要动手。
WORK = [
    "看一下广告数据",
    "把 UK 站这个活动预算改成 9 镑",
    "帮我查一下 B0C1234567",
    "跑一下测试",
    "改一下这个文件的样式",
    "我的店铺最近怎么样",
    "这个 listing 怎么优化",
    "http://example.com 这个页面打不开",
    "测试",                      # 可能是"跑测试"，宁可走全量
    "帮我看看",                   # 有动作词，判不准就走安全那条
    "分析一下",
    "昨天的订单有多少",
    # 一个字都不在业务词表里，却是标准的知识库问题 —— 黑名单列不全，
    # 所以闲聊走白名单。这条曾经被判成闲聊，于是知识库根本没查。
    "新卖家注册身份验证失败怎么办",
    "为什么我的账号被审核了",
    "你好，帮我查一下广告",     # 有寒暄但不止寒暄
]


@pytest.mark.parametrize("text", CHAT)
def test_chat_lane(text):
    r = routing.classify(text)
    assert r.lane == "chat", f"{text!r} 被判成 {r.lane}（{r.reason}）"
    assert routing.tools_for(r, ["t1", "t2"]) == []


@pytest.mark.parametrize("text", WORK)
def test_work_lane(text):
    r = routing.classify(text)
    assert r.lane == "work", f"{text!r} 被判成 {r.lane}（{r.reason}）"
    # 常规路线绝不裁工具
    assert routing.tools_for(r, ["t1", "t2"]) == ["t1", "t2"]


def test_board_lane_names_a_real_tool():
    r = routing.classify("帮我做个市场调研 B0C1234567", ops_bridge=True)
    assert r.lane == "board"
    assert r.board_tool == "market_generate_report"
    hint = routing.board_hint(r)
    assert "market_generate_report" in hint and "第一步就调用" in hint
    # 板块任务照挂全量工具
    assert routing.tools_for(r, ["t1"]) == ["t1"]


def test_board_only_when_bridge_is_on():
    """没接 IvyeaOps 时不存在板块工具，别给一条调不到的指令。"""
    assert routing.classify("帮我做个市场调研", ops_bridge=False).lane != "board"


@pytest.mark.parametrize("text,tool", [
    ("给我一个打法", "playbook_generate_report"),
    ("做个关键词竞争分析", "deep_generate_report"),
    ("跑个广告巡检", "ad_audit_start"),
])
def test_board_intent_table(text, tool):
    assert routing.classify(text, ops_bridge=True).board_tool == tool


def test_injected_blocks_do_not_change_the_lane():
    """判据只看用户打的那句话 —— 注入的技能手册/知识证据不算数。

    这正是上一次事故的形状：一句「你好」后面被贴了 1600 字技能手册，
    整段拿去判定，于是"简单"变"复杂"。
    """
    noisy = ("你好\n\n[Ivyea Skill：本轮相关可复用流程]\n"
             + "执行 分析 优化 检查 广告 listing " * 40)
    assert routing.classify(noisy).lane == "chat"


def test_attachments_never_take_the_fast_lane():
    assert routing.classify("你好", has_attachments=True).lane == "work"


def test_long_message_never_takes_the_fast_lane():
    assert routing.classify("你好呀" * 30).lane == "work"
