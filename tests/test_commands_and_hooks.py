"""Custom slash commands + user hooks."""
from __future__ import annotations

import json

from ivyea_agent import commands, config, hooks


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
