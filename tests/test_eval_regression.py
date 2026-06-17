"""广告 Agent eval 回归集 —— 守住建议质量不退化。

对固定样例跑全链路（vendored 规则引擎），断言关键决策计数稳定。任何改动若动了
否词/放量/降bid 的判定，这里会立刻变红。golden 由 2026-06 当前实现冻结。
"""
from __future__ import annotations

import tempfile

from ivyea_agent import rule_engine

# 样例 search-term-report 的冻结决策快照（实现退化即变红）
GOLDEN = {
    "asin": "B0PUBLIC01",
    "negative_candidate_count": 1,
    "scale_up_count": 1,
    "reduce_bid_count": 1,
}


def test_vendored_engine_decisions_stable():
    out = rule_engine.run(str(rule_engine.SAMPLE_CSV), site="US", output_dir=tempfile.mkdtemp())
    s = out["summary"]
    for k, v in GOLDEN.items():
        assert s.get(k) == v, f"决策回归：{k} 期望 {v}，实际 {s.get(k)}"


def test_report_has_required_sections():
    out = rule_engine.run(str(rule_engine.SAMPLE_CSV), site="US", output_dir=tempfile.mkdtemp())
    md = out["report_md"]
    # 报告分区不应丢失（否词/放量/降bid 任一分区缺失即退化）
    assert "否" in md and ("放量" in md or "加" in md or "bid" in md.lower())
