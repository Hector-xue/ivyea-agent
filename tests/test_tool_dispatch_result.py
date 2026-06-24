"""Structured tool dispatch result (ok / text / traceback)."""
from __future__ import annotations

from ivyea_agent import agent_tools
from ivyea_agent.agent_tools import ToolContext, dispatch, dispatch_result


def test_success_is_ok_without_traceback():
    res = dispatch_result("list_dir", {"path": "."}, ToolContext())
    assert res.ok is True
    assert res.error == ""
    assert isinstance(res.text, str)


def test_unknown_tool_is_not_ok():
    res = dispatch_result("does_not_exist", {}, ToolContext())
    assert res.ok is False
    assert "未知工具" in res.text


def test_exception_captures_traceback_but_hides_it_from_text(monkeypatch):
    def boom(args, ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(agent_tools._DISPATCH, "boom", boom)
    res = dispatch_result("boom", {}, ToolContext())
    assert res.ok is False
    assert "kaboom" in res.text and "执行出错" in res.text
    assert "Traceback" in res.error          # full traceback retained for debugging
    assert "Traceback" not in res.text       # but not surfaced to the model


def test_dispatch_string_wrapper_is_backward_compatible():
    out = dispatch("list_dir", {"path": "."}, ToolContext())
    assert isinstance(out, str)
