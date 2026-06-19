"""Account-level diagnosis: waste, winners, listing gaps."""
from __future__ import annotations

import csv

from ivyea_agent import account_diagnosis as ad


ROWS = [
    {"ASIN": "B0A", "Campaign Name": "SP A Broad", "Customer Search Term": "wireless microphone replacement",
     "Impressions": "900", "Clicks": "22", "Spend": "33", "Orders": "0", "Sales": "0"},
    {"ASIN": "B0A", "Campaign Name": "SP A Exact", "Customer Search Term": "karaoke machine with screen",
     "Impressions": "1200", "Clicks": "30", "Spend": "24", "Orders": "4", "Sales": "240"},
    {"ASIN": "B0A", "Campaign Name": "SP A Exact", "Customer Search Term": "portable karaoke machine",
     "Impressions": "1000", "Clicks": "20", "Spend": "40", "Orders": "1", "Sales": "50"},
    {"ASIN": "B0B", "Campaign Name": "SP B Low CTR", "Customer Search Term": "party speaker",
     "Impressions": "2000", "Clicks": "2", "Spend": "3", "Orders": "0", "Sales": "0"},
]


def _write_csv(tmp_path):
    p = tmp_path / "ads.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(ROWS[0].keys()))
        w.writeheader()
        w.writerows(ROWS)
    return str(p)


def test_diagnose_priorities_and_buckets(tmp_path):
    res = ad.diagnose(_write_csv(tmp_path), target_acos=0.3, listing_text="karaoke machine")
    assert res["total"]["clicks"] == 74
    assert res["no_order"][0]["name"] == "wireless microphone replacement"
    assert res["winners"][0]["name"] == "karaoke machine with screen"
    assert res["high_acos"][0]["name"] == "portable karaoke machine"
    assert res["listing_gaps"][0]["missing_tokens"] == ["screen"]
    assert any(p["level"] == "P0" for p in res["priority"])


def test_render_md_contains_sections(tmp_path):
    text = ad.render_md(ad.diagnose(_write_csv(tmp_path), target_acos=0.3))
    assert "# 亚马逊广告账户诊断" in text
    assert "## 高点击零单浪费" in text
    assert "wireless microphone replacement" in text


def test_agent_tool_registered():
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "run_account_diagnosis" in names
    assert "run_account_diagnosis" in _DISPATCH
