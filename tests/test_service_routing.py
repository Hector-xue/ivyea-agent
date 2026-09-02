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
    assert "route.is_chat or route.is_quick or route.is_board" in text
    assert "ctx.progress_reporting_disabled = True" in text


def test_cli_wires_routing_into_every_turn_call():
    """终端那条路也要接上，而且**每一个** run_turn_stream 调用点都要带 tools。

    cli.py 里有四个调用点（TUI/-p 共用一个，行式循环三个，按渲染模式分叉）。
    漏掉任何一个，那种渲染模式下闲聊就又挂满 54 个工具了 —— 而这种漏接跑一次
    正常对话是看不出来的（工具挂着不用，只是慢）。所以在源码层面钉死。
    """
    from pathlib import Path

    from ivyea_agent import cli

    src = Path(cli.__file__).read_text(encoding="utf-8")
    # 不去解析括号（调用里嵌着 lambda，配对很脆）。直接对数量：**每一个调用点都要带一次**。
    calls = src.count("agent_loop.run_turn_stream(")
    wired = src.count("tools=routing.tools_for(route)")
    assert calls >= 4, f"调用点数量变了（{calls}），这条用例要跟着更新"
    assert wired == calls, f"{calls} 个调用点只接了 {wired} 个"
    # 路线判定本身：ctx 跨轮复用，所以必须**每轮赋值**（含赋回 False）
    assert src.count(
        "ctx.progress_reporting_disabled = route.is_chat or route.is_quick or route.is_board") == 2


# ── quick：知识型提问只挂只读检索工具 ────────────────────────────────────────
def test_quick_lane_takes_knowledge_questions():
    """"这是什么/了解过吗/有什么区别" 这类问题走 quick。"""
    from ivyea_agent import routing

    for msg in ("你了解过 51WORLD 这家公司吗", "ACOS 是什么意思", "什么是 A9 算法",
                "介绍一下亚马逊的 FBA", "FBA 和 FBM 有什么区别", "变体是干什么的"):
        assert routing.classify(msg).lane == "quick", msg


def test_quick_lane_refuses_anything_touching_my_own_data():
    """凡是要看用户自己的数据、或者要动手的，一律退回 work。

    quick 判错的代价比 chat 小（答案照样是查过才给的），但"没有写工具"这件事在
    用户要动手时就是硬伤 —— 所以这道闸只能往 work 那边倒。
    """
    from ivyea_agent import routing

    for msg in ("我的广告 ACOS 是什么情况", "店铺最近的销量是什么水平",
                "这个报表里的转化率是什么意思", "帮我看看什么是问题所在",
                "这段代码里的这个函数是什么作用", "把配置文件里的这项是什么改一下"):
        assert routing.classify(msg).lane == "work", msg


def test_quick_lane_is_not_triggered_by_plain_statements():
    """不是提问形态的，一律 work —— 判不准往"多做一点"那边倒。"""
    from ivyea_agent import routing

    for msg in ("新卖家注册身份验证失败怎么办", "广告怎么优化否词", "跑一下巡检",
                "51WORLD 这家公司挺不错的"):
        assert routing.classify(msg).lane == "work", msg


def test_quick_tool_set_is_readonly_and_much_smaller():
    """quick 的工具集必须又小又干净：一个写/执行/板块工具都不能混进去。"""
    import json

    from ivyea_agent import routing
    from ivyea_agent.agent_tools import READONLY_TOOLS, TOOL_SCHEMAS

    quick = routing.quick_tool_schemas()
    names = {t["function"]["name"] for t in quick}
    assert names, "quick 工具集不能是空的 —— 那是 chat 的行为"
    assert names <= READONLY_TOOLS, f"混进了非只读工具：{names - READONLY_TOOLS}"
    assert len(quick) < len(TOOL_SCHEMAS) / 4
    # 省下来的 token 是这条 lane 的全部意义，钉一个下限防止有人往里加工具加回去
    assert len(json.dumps(quick, ensure_ascii=False)) < \
        len(json.dumps(TOOL_SCHEMAS, ensure_ascii=False)) / 4


def test_quick_lane_keeps_searching_unlike_chat():
    """quick 和 chat 的分界：chat 不挂工具，quick 照样能查。"""
    from ivyea_agent import routing

    chat = routing.classify("你好")
    quick = routing.classify("什么是 A9 算法")
    assert routing.tools_for(chat) == []
    assert len(routing.tools_for(quick) or []) >= 5


def test_quick_lane_is_wired_into_service_tool_selection():
    """接线钉死：serve 的 _tools_for 必须认 quick，否则裁工具这件事根本没发生。"""
    from pathlib import Path

    from ivyea_agent import service

    src = Path(service.__file__).read_text(encoding="utf-8")
    assert "route.is_quick" in src and "routing.quick_tool_schemas()" in src


def test_quick_hint_tells_the_model_to_search_at_most_once():
    """裁工具省的是每步 token，省不掉步数 —— 步数得靠这条提示。

    实测反例：挂上只读工具集之后模型仍然跑了 web_search → web_images →
    web_search → web_images，后两步查的和前两步是同一件事，白白多两次往返。
    """
    from ivyea_agent import routing

    hint = routing.quick_hint(routing.classify("什么是 A9 算法"))
    assert "最多一轮" in hint and "第二遍" in hint
    assert routing.quick_hint(routing.classify("你好")) == ""
    assert routing.quick_hint(routing.classify("把测试跑一遍")) == ""


def test_quick_hint_is_wired_everywhere_board_hint_is():
    """board_hint 有三个注入点（serve 一个、CLI 两个），quick_hint 一个都不能漏。"""
    from pathlib import Path

    from ivyea_agent import cli, service

    for mod in (service, cli):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert src.count("routing.board_hint(route)") == src.count("routing.quick_hint(route)"), \
            f"{mod.__name__}: board_hint 和 quick_hint 的注入点数量对不上"
