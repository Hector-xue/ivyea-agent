"""memory：决策记录、历史否决、稳定期、记要点、检索 —— 临时 DB。"""
from __future__ import annotations

import time


def _mem():
    from ivyea_agent import memory
    return memory


def test_record_and_was_rejected(ivyea_home):
    memory = _mem()
    memory.record_decision("B0TEST", "karaoke remote", "negative", "reject")
    assert memory.was_rejected("B0TEST", "karaoke remote", "negative") is True
    assert memory.was_rejected("B0TEST", "other", "negative") is False


def test_latest_decision_wins(ivyea_home):
    memory = _mem()
    memory.record_decision("B0X", "term", "negative", "reject")
    time.sleep(0.01)
    memory.record_decision("B0X", "term", "negative", "approve")
    assert memory.was_rejected("B0X", "term", "negative") is False


def test_days_since_last_approve(ivyea_home):
    memory = _mem()
    memory.record_decision("B0X", "kw", "reduce_bid", "approve")
    d = memory.days_since_last_approve("B0X", "kw")
    assert d is not None and d < 1
    assert memory.days_since_last_approve("B0X", "never") is None


def test_remember_and_search(ivyea_home):
    memory = _mem()
    memory.remember("旺季前两周提前放量核心词", asin="B0X")
    hits = memory.search("放量")
    assert any("放量" in h["text"] for h in hits)
    note = memory.read_note("B0X")
    assert "放量" in note


def test_annotate_blocks_rejected(ivyea_home):
    from ivyea_agent.actions import Action
    memory = _mem()
    memory.record_decision("B0X", "bad term", "negative", "reject")
    acts = [Action(kind="negative", search_term="bad term", asin="B0X")]
    out = memory.annotate(acts, "B0X")
    assert out[0].blocked is True
    assert "否决" in out[0].block_reason


def test_stats_shape(ivyea_home):
    memory = _mem()
    memory.record_decision("B0X", "t", "negative", "approve")
    s = memory.stats()
    assert s["decisions"] >= 1 and "fts" in s
