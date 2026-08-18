"""serve 侧的路线接线：三条 lane 各自到底改变了什么。

只测"routing.classify 判得对"是不够的 —— 判对了但没接上去，用户那边一秒都省不下来。
这里用桩 provider 把真实的 chat_stream 跑起来，检查**实际发给模型的东西**。
"""
from __future__ import annotations

import json


class _EchoProvider:
    """记下每次调用拿到的 messages / tools，然后回一句话收尾。"""

    def __init__(self):
        self.calls: list[tuple[list, list | None]] = []

    def stream_chat(self, messages, tools=None):
        self.calls.append(([dict(m) for m in messages], tools))
        yield {"type": "text", "text": "好的。"}
        yield {"type": "final", "content": "好的。", "tool_calls": [], "usage": {}}

    # 非流式入口（有的路径会走它）
    def chat(self, messages, tools=None):
        self.calls.append(([dict(m) for m in messages], tools))
        return {"content": "好的。", "tool_calls": []}


def _run(message: str, **payload):
    from ivyea_agent import service

    provider = _EchoProvider()
    events: list[tuple[str, dict]] = []
    body = {"message": message, "persist": False, "max_steps": 2, **payload}
    result = service.chat_stream(body, lambda e, d: events.append((e, d)), provider=provider)
    start = next(d for e, d in events if e == "start")
    return result, provider, start


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            return json.dumps(content, ensure_ascii=False)
    return ""


def test_chat_lane_sends_no_tools_and_no_injections(ivyea_home):
    """闲聊：不挂工具、不注知识、不注技能 —— 一次调用就该结束。"""
    result, provider, start = _run("你好", auto_skill=True)

    assert result["ok"] is True
    assert start["lane"] == "chat"
    assert len(provider.calls) == 1, "闲聊不该走第二轮"
    messages, tools = provider.calls[0]
    assert tools == [], "闲聊路线必须一个工具都不挂"
    said = _last_user_text(messages)
    assert "[Ivyea 本地知识检索" not in said
    assert "[Ivyea Skill" not in said


def test_work_lane_keeps_everything(ivyea_home):
    """常规路线一个字都不能变：全量工具（tools=None 交给 agent_loop 兜底）。"""
    _, provider, start = _run("新卖家注册身份验证失败怎么办")

    assert start["lane"] == "work"
    _, tools = provider.calls[0]
    # run_turn_stream 会把 tools=None 兜底成全量 TOOL_SCHEMAS，所以 provider 看到的是整份
    assert tools and len(tools) > 10, "常规路线不裁工具"


def test_board_lane_points_at_the_tool_and_keeps_tools(ivyea_home):
    """板块任务：给出点名到工具的直达指令，但工具集照样是全量。"""
    _, provider, start = _run(
        "帮我做个市场调研 B0C1234567",
        ops_bridge={"base_url": "http://127.0.0.1:8001"},
    )

    assert start["lane"] == "board"
    messages, tools = provider.calls[0]
    assert tools and len(tools) > 10, "板块任务裁工具 = 可能缺能力，不划算"
    said = _last_user_text(messages)
    assert "[本轮直达]" in said and "market_generate_report" in said


def test_board_and_chat_switch_off_the_reporting_state_machine(ivyea_home):
    """这两条 lane 都不该被 todo/阶段汇报挡路（一句「测试」曾 18 步里 17 步花在这）。"""
    from ivyea_agent import routing

    for message, kwargs in (("你好", {}),
                            ("帮我做个市场调研", {"ops_bridge": {"base_url": "x"}})):
        route = routing.classify(message, ops_bridge=bool(kwargs.get("ops_bridge")))
        assert route.lane in ("chat", "board")
    # 接线本身：chat_stream 里对这两条 lane 关掉 progress_reporting_disabled
    src = __import__("ivyea_agent.service", fromlist=["service"]).__file__
    text = open(src, encoding="utf-8").read()
    assert "route.is_chat or route.is_board" in text
    assert "ctx.progress_reporting_disabled = True" in text
