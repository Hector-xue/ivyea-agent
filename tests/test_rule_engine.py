"""vendored CSV 规则引擎回归：跑样例 CSV 出报告（兜底路径不退化）。"""
from __future__ import annotations

from ivyea_agent import rule_engine


def test_vendored_engine_on_sample(tmp_path):
    out = rule_engine.run(str(rule_engine.SAMPLE_CSV), site="US", output_dir=str(tmp_path))
    assert out["report_md"].strip()           # 有报告
    assert "summary" in out                    # 有结构化摘要
    assert out["files"].get("report_md")       # 报告落盘
