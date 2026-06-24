"""Mid-turn auto-compaction guard (overflow protection at step boundaries)."""
from __future__ import annotations

from ivyea_agent import agent_loop, config, context


class FakeProvider:
    def complete(self, system, user, **kw):
        return "SUMMARY: kept the key facts"


def test_estimate_tokens_rough():
    assert context.estimate_tokens([{"role": "user", "content": "x" * 300}]) == 100


def test_hard_ceiling_triggers_even_with_auto_compact_off(monkeypatch):
    monkeypatch.setattr(config, "get_setting",
                        lambda k, d=None: {"compact_hard_ceiling_tokens": 50, "auto_compact": False}.get(k, d))
    assert context.should_compact_midturn(100) is True   # over ceiling -> protect
    assert context.should_compact_midturn(10) is False    # under ceiling, auto off


def test_maybe_compact_replaces_in_place(monkeypatch):
    monkeypatch.setattr(config, "get_setting",
                        lambda k, d=None: {"compact_hard_ceiling_tokens": 10}.get(k, d))
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u" * 60},
        {"role": "assistant", "content": "a" * 60},
        {"role": "user", "content": "b" * 60},
        {"role": "assistant", "content": "c" * 60},
    ]
    agent_loop._maybe_compact(messages, FakeProvider(), step_idx=1, narrate=lambda s: None)
    assert messages[0]["role"] == "system"            # system preserved
    assert len(messages) == 3                          # system + summary + ack
    assert any("摘要" in (m.get("content") or "") for m in messages)
    # no orphaned tool pairing left behind
    assert not any(m.get("tool_calls") for m in messages)


def test_maybe_compact_skips_first_step():
    messages = [{"role": "user", "content": "x" * 999999}]
    before = [dict(m) for m in messages]
    agent_loop._maybe_compact(messages, FakeProvider(), step_idx=0, narrate=lambda s: None)
    assert messages == before
