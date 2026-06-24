"""Read-only subagent tool (dispatch_subagent)."""
from __future__ import annotations

from ivyea_agent.agent_tools import TOOL_SCHEMAS, ToolContext, _subagent_schemas, dispatch


class FakeProvider:
    """Ends the sub-loop immediately with a final answer; records the tools it got."""
    def __init__(self):
        self.tools = None

    def chat(self, messages, tools=None):
        self.tools = tools
        return {"content": "结论：在 pkg/calc.py:2 实现。", "tool_calls": []}

    def complete(self, *a, **k):
        return ""


def test_subagent_returns_summary():
    out = dispatch("dispatch_subagent", {"task": "add 在哪实现"}, ToolContext(provider=FakeProvider()))
    assert "子 agent 结论" in out and "calc.py" in out


def test_subagent_without_provider_is_graceful():
    assert "无可用主脑" in dispatch("dispatch_subagent", {"task": "x"}, ToolContext())


def test_subagent_empty_task():
    assert "task 为空" in dispatch("dispatch_subagent", {"task": "  "}, ToolContext(provider=FakeProvider()))


def test_subagent_tools_are_readonly_and_non_recursive():
    fp = FakeProvider()
    dispatch("dispatch_subagent", {"task": "x"}, ToolContext(provider=fp))
    given = {t["function"]["name"] for t in (fp.tools or [])}
    assert "dispatch_subagent" not in given            # cannot spawn nested subagents
    assert {"write_file", "edit_file", "run_command", "execute_actions"} & given == set()
    assert {"grep", "read_file", "code_search"} <= given


def test_subagent_schema_subset_matches():
    names = {t["function"]["name"] for t in _subagent_schemas()}
    assert "dispatch_subagent" not in names
    assert "dispatch_subagent" in {t["function"]["name"] for t in TOOL_SCHEMAS}
