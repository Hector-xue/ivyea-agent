"""Custom slash commands + user hooks."""
from __future__ import annotations

import json
import os

import pytest

from ivyea_agent import commands, config, hooks

_posix_only = pytest.mark.skipif(os.name == "nt", reason="钩子命令用了 bash 语法（; / 引号转义），cmd 不等价")


def _use_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IVYEA_DIR", tmp_path)
    return tmp_path


def test_expand_with_arguments(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    (home / "commands").mkdir()
    (home / "commands" / "audit.md").write_text(
        "审计这个 ASIN：$ARGUMENTS，输出否词与放量建议。", encoding="utf-8")
    out = commands.expand("audit", "B0ABCDEFGH")
    assert out == "审计这个 ASIN：B0ABCDEFGH，输出否词与放量建议。"


def test_expand_appends_when_no_placeholder(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    (home / "commands").mkdir()
    (home / "commands" / "weekly.md").write_text("做本周运营复盘。", encoding="utf-8")
    assert commands.expand("weekly", "店铺 1863") == "做本周运营复盘。\n\n店铺 1863"


def test_expand_unknown_and_reserved(tmp_path, monkeypatch):
    _use_home(tmp_path, monkeypatch)
    assert commands.expand("nope", "x") is None
    assert commands.expand("help", "x") is None        # reserved built-in


def test_list_commands(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    (home / "commands").mkdir()
    (home / "commands" / "a.md").write_text("first line summary\nbody", encoding="utf-8")
    listed = commands.list_commands()
    assert listed.get("a") == "first line summary"


def test_hooks_zero_overhead_when_unconfigured(tmp_path, monkeypatch):
    _use_home(tmp_path, monkeypatch)
    hooks.reload()
    assert hooks.enabled() is False
    hooks.fire("user_prompt", {"prompt": "hi"})   # must be a no-op, not raise


def test_hooks_fire_runs_configured_command(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    marker = home / "fired.txt"
    # `echo` + redirection works on both bash and cmd; json.dumps escapes the path safely.
    (home / "hooks.json").write_text(
        json.dumps({"user_prompt": [f"echo fired > {marker}"]}), encoding="utf-8")
    hooks.reload()
    assert hooks.enabled() is True
    hooks.fire("user_prompt", {"prompt": "hi"})
    assert marker.exists()


def test_hooks_invalid_event_ignored(tmp_path, monkeypatch):
    _use_home(tmp_path, monkeypatch)
    hooks.reload()
    hooks.fire("not_an_event", {})   # no raise


def _write_hooks(home, cfg: dict):
    (home / "hooks.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    hooks.reload()


@_posix_only
def test_fire_decision_exit2_blocks_with_stderr_reason(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    _write_hooks(home, {"pre_tool_use": [
        {"matcher": "run_command", "command": "echo denied by guard >&2; exit 2"}]})
    ok, reason = hooks.fire_decision("pre_tool_use", {"tool_name": "run_command"},
                                     tool_name="run_command", readonly=False)
    assert ok is False and "denied by guard" in reason


@_posix_only
def test_fire_decision_stdout_json_block(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    _write_hooks(home, {"pre_tool_use": [
        {"matcher": "write_file", "command": 'echo {\\"decision\\":\\"block\\",\\"reason\\":\\"no writes\\"}'}]})
    ok, reason = hooks.fire_decision("pre_tool_use", {}, tool_name="write_file", readonly=False)
    assert ok is False and reason == "no writes"


def test_fire_decision_fail_open(tmp_path, monkeypatch):
    """exit 0/1、命令不存在、非 JSON stdout → 一律放行。"""
    home = _use_home(tmp_path, monkeypatch)
    _write_hooks(home, {"pre_tool_use": [
        {"matcher": ".*", "command": "exit 1"},
        {"matcher": ".*", "command": "/no/such/binary-xyz"},
        {"matcher": ".*", "command": "echo not-json"}]})
    ok, reason = hooks.fire_decision("pre_tool_use", {}, tool_name="anything", readonly=False)
    assert ok is True and reason == ""


def test_pre_tool_use_str_entry_skips_readonly(tmp_path, monkeypatch):
    """字符串条目（无 matcher）默认只拦非只读工具。"""
    home = _use_home(tmp_path, monkeypatch)
    _write_hooks(home, {"pre_tool_use": ["exit 2"]})
    ok, _ = hooks.fire_decision("pre_tool_use", {}, tool_name="read_file", readonly=True)
    assert ok is True                     # 只读被跳过
    ok, _ = hooks.fire_decision("pre_tool_use", {}, tool_name="write_file", readonly=False)
    assert ok is False                    # 非只读被拦


def test_matcher_regex_scopes_tools(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    _write_hooks(home, {"pre_tool_use": [{"matcher": "write_file|edit_file", "command": "exit 2"}]})
    assert hooks.fire_decision("pre_tool_use", {}, tool_name="edit_file", readonly=False)[0] is False
    assert hooks.fire_decision("pre_tool_use", {}, tool_name="run_command", readonly=False)[0] is True


@_posix_only
def test_dispatch_result_pre_hook_blocks_and_post_hook_fires(tmp_path, monkeypatch):
    """dispatch_result 集成：pre 拒绝短路；未拦工具照常执行且触发 post_tool_use。"""
    home = _use_home(tmp_path, monkeypatch)
    post_marker = home / "post.txt"
    _write_hooks(home, {
        "pre_tool_use": [{"matcher": "run_command", "command": "echo guard says no >&2; exit 2"}],
        "post_tool_use": [{"matcher": "list_dir", "command": f"echo done > {post_marker}"}],
    })
    from ivyea_agent import agent_tools
    ctx = agent_tools.ToolContext()
    res = agent_tools.dispatch_result("run_command", {"command": "echo hi"}, ctx)
    assert res.ok is False and "pre_tool_use hook 拒绝" in res.text and "guard says no" in res.text
    res2 = agent_tools.dispatch_result("list_dir", {"path": str(home)}, ctx)
    assert res2.ok is True
    assert post_marker.exists()


def test_legacy_list_format_still_works_for_tool_events(tmp_path, monkeypatch):
    """旧 list[str] 与新 dict 条目混用不炸。"""
    home = _use_home(tmp_path, monkeypatch)
    marker = home / "mix.txt"
    _write_hooks(home, {"post_tool_use": [f"echo x > {marker}",
                                          {"matcher": "list_dir", "command": "true"}]})
    hooks.fire("post_tool_use", {}, tool_name="write_file", readonly=False)
    assert marker.exists()


def test_chat_p_fires_stop_and_session_end(tmp_path, monkeypatch, ivyea_home, capsys):
    """-p 全链路：user_prompt → stop → session_end 按序触发。"""
    from ivyea_agent import providers, hooks as hooks_mod
    from ivyea_agent.cli import build_parser
    fired = []
    monkeypatch.setattr(hooks_mod, "fire", lambda ev, payload=None, **kw: fired.append(ev))

    class _P:
        def stream_chat(self, messages, tools=None, **kw):
            yield {"type": "final", "content": "ok", "tool_calls": [], "usage": {}}

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(providers, "build_chain", lambda mcfg, key, narrate=None: _P())
    parser = build_parser()
    args = parser.parse_args(["chat", "-p", "hi"])
    assert args.func(args) == 0
    assert [e for e in fired if e in ("user_prompt", "stop", "session_end")] == \
        ["user_prompt", "stop", "session_end"]
    assert "session_start" in fired
