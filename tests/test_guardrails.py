"""guardrails：硬护栏拦截逻辑（纯函数，无 IO）。"""
from __future__ import annotations

from ivyea_agent import guardrails
from ivyea_agent.actions import Action


def test_protected_term_blocked():
    a = Action(kind="negative", search_term="my brand")
    out = guardrails.annotate([a], protected_terms=["my brand"])
    assert out[0].blocked and "保护词" in out[0].block_reason


def test_no_negate_brand_competitor():
    for cat in ("brand_term", "competitor_term", "core_category_term", "asin_term"):
        a = Action(kind="negative", search_term="x", term_category=cat)
        out = guardrails.annotate([a])
        assert out[0].blocked, f"{cat} 应被拦"


def test_negate_normal_passes():
    a = Action(kind="negative", search_term="random junk", term_category="generic_term",
               confidence="high")
    out = guardrails.annotate([a])
    assert not out[0].blocked


def test_low_confidence_blocked():
    a = Action(kind="negative", search_term="x", term_category="generic_term", confidence="low")
    out = guardrails.annotate([a])
    assert out[0].blocked


def test_bid_over_20pct_blocked():
    a = Action(kind="reduce_bid", search_term="kw", term_category="generic_term",
               confidence="high", change_pct=-0.25)
    out = guardrails.annotate([a])
    assert out[0].blocked and "20%" in out[0].block_reason


def test_bid_within_limit_passes():
    a = Action(kind="reduce_bid", search_term="kw", term_category="generic_term",
               confidence="high", change_pct=-0.15)
    out = guardrails.annotate([a])
    assert not out[0].blocked


def test_core_category_no_reduce_bid():
    a = Action(kind="reduce_bid", search_term="kw", term_category="core_category_term",
               confidence="high", change_pct=-0.15)
    out = guardrails.annotate([a])
    assert out[0].blocked
