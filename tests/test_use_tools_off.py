"""不挂工具的纯文本轮次（IvyeaOps 把 agent 当文本引擎用时走这条）。

以前 `tools or TOOL_SCHEMAS` 把 [] 当假值退回全量工具，等于关不掉：模型会花步数
去查工具（plan_mode 下 MCP 调用还会被拒），中间叙述被逐 token 流成"报告"。
"""
from __future__ import annotations

from ivyea_agent import agent_loop, service
from ivyea_agent.agent_tools import ToolContext


class _Prov:
    def __init__(self):
        self.seen = []

    def chat(self, messages, tools=None, **kw):
        self.seen.append(tools)
        return {"content": "报告正文", "tool_calls": []}

    def stream_chat(self, messages, tools=None, **kw):
        self.seen.append(tools)
        yield {"type": "text", "text": "报告正文"}
        yield {"type": "final", "content": "报告正文", "tool_calls": [], "usage": {}}


def test_empty_tools_list_means_no_tools():
    prov = _Prov()
    out = agent_loop.run_turn(prov, ToolContext(), [{"role": "user", "content": "写报告"}], tools=[])
    assert out == "报告正文"
    assert prov.seen == [[]]                      # 空工具集透传给 provider，不回落全量


def test_empty_tools_list_means_no_tools_streaming():
    prov = _Prov()
    out = agent_loop.run_turn_stream(prov, ToolContext(), [{"role": "user", "content": "写报告"}],
                                     tools=[], render=lambda _t: None)
    assert out["text"] == "报告正文"
    assert prov.seen == [[]]


def test_default_still_gets_the_full_tool_set():
    prov = _Prov()
    agent_loop.run_turn(prov, ToolContext(), [{"role": "user", "content": "hi"}])
    assert prov.seen[0] is agent_loop.TOOL_SCHEMAS


def test_service_payload_flag():
    assert service._tools_for({"use_tools": False}) == []
    assert service._tools_for({"use_tools": True}) is None
    assert service._tools_for({}) is None          # 默认不变：带全量工具
