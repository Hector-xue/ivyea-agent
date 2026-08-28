"""Mid-turn auto-compaction guard (overflow protection at step boundaries)."""
from __future__ import annotations

from ivyea_agent import agent_loop, config, context


class FakeProvider:
    def complete(self, system, user, **kw):
        return "SUMMARY: kept the key facts"


def test_estimate_tokens_rough():
    # CJK/其它分开计：300 英文字符 ≈ 79 tok，300 汉字 ≈ 225 tok（旧 chars//3 对中文低估近半）
    assert 70 <= context.estimate_tokens([{"role": "user", "content": "x" * 300}]) <= 90
    assert 200 <= context.estimate_tokens([{"role": "user", "content": "中" * 300}]) <= 250


def test_hard_ceiling_triggers_even_with_auto_compact_off(monkeypatch):
    monkeypatch.setattr(config, "get_setting",
                        lambda k, d=None: {"compact_hard_ceiling_tokens": 50, "auto_compact": False}.get(k, d))
    assert context.should_compact_midturn(100) is True   # over ceiling -> protect
    assert context.should_compact_midturn(10) is False    # under ceiling, auto off


def test_maybe_compact_replaces_in_place(monkeypatch):
    monkeypatch.setattr(config, "get_setting",
                        lambda k, d=None: {"compact_hard_ceiling_tokens": 10}.get(k, d))
    messages = [
        # 每条 2200 字符（合计 ≈2300 tok）：自动压缩这条路上有 `worth_compacting` 闸，
        # 可压段不足 MIN_COMPACTIBLE_TOKENS 就不跑。别把它缩回去 —— 缩了这个用例测的
        # 就不是压缩，而是那道闸。
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u" * 2200},
        {"role": "assistant", "content": "a" * 2200},
        {"role": "user", "content": "b" * 2200},
        {"role": "assistant", "content": "c" * 2200},
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


# ── 阈值低于 system 提示词时的"反复空压" ──────────────────────────────────
class CountingProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, system, user, **kw):
        self.calls += 1
        return "SUMMARY"


def _tiny_threshold(monkeypatch):
    """把 compact_at_tokens 调到比 system 提示词还低 —— 小上下文模型上真实会发生。"""
    monkeypatch.setattr(config, "get_setting",
                        lambda k, d=None: {"compact_at_tokens": 100, "auto_compact": True,
                                           "compact_hard_ceiling_tokens": 10_000_000}.get(k, d))


def _msgs():
    # system 一条就 5000 字符（≈1300 tok），历史很短 —— 压缩动不了 system，
    # 于是用量永远在 100 tok 的阈值之上。
    return [{"role": "system", "content": "S" * 5000},
            {"role": "user", "content": "u" * 40},
            {"role": "assistant", "content": "a" * 40},
            {"role": "user", "content": "b" * 40},
            {"role": "assistant", "content": "c" * 40}]


def test_threshold_below_system_prompt_does_not_burn_calls(monkeypatch):
    """回归：阈值压在 system 之下时，每一步都判定"该压"、每一步都白压。"""
    _tiny_threshold(monkeypatch)
    messages = _msgs()
    before = [dict(m) for m in messages]
    provider = CountingProvider()
    assert context.should_compact_midturn(context.estimate_tokens(messages)) is True   # 阈值确实到了
    assert context.worth_compacting(messages) is False                                  # 但压不动
    for step in range(1, 6):
        agent_loop._maybe_compact(messages, provider, step_idx=step, narrate=lambda s: None)
    assert provider.calls == 0        # 一次摘要调用都不该发生
    assert messages == before          # 历史一个字都没动


def test_unsatisfiable_threshold_is_reported_once(monkeypatch):
    """默不作声地忽略用户设的阈值，比压错更难查 —— 但也只说一次。"""
    _tiny_threshold(monkeypatch)
    said = []
    status = agent_loop.TurnStatus(max_steps=10)
    for step in range(1, 5):
        agent_loop._maybe_compact(_msgs(), CountingProvider(), step_idx=step,
                                  narrate=said.append, status=status)
    assert len(said) == 1
    assert "compact_at_tokens" in said[0]


def test_manual_compact_is_not_gated(monkeypatch):
    """闸只管自动压缩。用户手敲 /compact 就是明确要求，短历史也照压。"""
    _tiny_threshold(monkeypatch)
    provider = CountingProvider()
    new, summary = context.compact(_msgs(), provider)
    assert provider.calls == 1 and summary == "SUMMARY"
    assert len(new) == 3               # system + 摘要 + 确认


def test_worth_compacting_says_yes_when_history_is_real(monkeypatch):
    _tiny_threshold(monkeypatch)
    messages = [{"role": "system", "content": "S" * 5000}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 3000} for i in range(4)]
    assert context.worth_compacting(messages) is True
