"""actions：从规则引擎明细 CSV 抽可执行动作。"""
from __future__ import annotations

import csv

from ivyea_agent import actions

_ROWS = [
    # decision_tag, search_term, term_category, match_type, confidence_level, decision_reason,
    # 30d_clicks, 30d_orders, 30d_spend, 30d_acos
    {"decision_tag": "negative_candidate", "search_term": "junk word", "term_category": "generic_term",
     "match_type": "BROAD", "confidence_level": "high", "decision_reason": "20点击0单",
     "30d_clicks": "20", "30d_orders": "0", "30d_spend": "12.5", "30d_acos": ""},
    {"decision_tag": "reduce_bid", "search_term": "pricey kw", "term_category": "generic_term",
     "match_type": "EXACT", "confidence_level": "high", "decision_reason": "高ACOS",
     "30d_clicks": "30", "30d_orders": "2", "30d_spend": "40", "30d_acos": "55%"},
    {"decision_tag": "observe", "search_term": "ignore me", "term_category": "generic_term",
     "match_type": "BROAD", "confidence_level": "low", "decision_reason": "观察",
     "30d_clicks": "3", "30d_orders": "0", "30d_spend": "1", "30d_acos": ""},
]


def _write_csv(tmp_path):
    p = tmp_path / "明细.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(_ROWS[0].keys()))
        w.writeheader()
        w.writerows(_ROWS)
    return str(p)


def test_extract_actions(tmp_path):
    acts = actions.extract_actions(_write_csv(tmp_path), asin="B0X")
    kinds = sorted(a.kind for a in acts)
    assert kinds == ["negative", "reduce_bid"]  # observe 被忽略
    neg = next(a for a in acts if a.kind == "negative")
    assert neg.search_term == "junk word" and neg.executable is True
    rb = next(a for a in acts if a.kind == "reduce_bid")
    assert rb.change_pct == actions.DEFAULT_REDUCE_PCT
    # 无当前 bid → 不可执行（只作建议）
    assert rb.new_bid is None and rb.executable is False


def test_reduce_bid_with_current_bid(tmp_path):
    acts = actions.extract_actions(_write_csv(tmp_path), current_bids={"pricey kw": 1.00}, asin="B0X")
    rb = next(a for a in acts if a.kind == "reduce_bid")
    assert rb.current_bid == 1.00 and rb.new_bid == 0.85 and rb.executable is True


def test_load_detail_from_dir(tmp_path):
    _write_csv(tmp_path)
    found = actions.load_detail_from_dir(str(tmp_path))
    assert found and found.endswith("明细.csv")
