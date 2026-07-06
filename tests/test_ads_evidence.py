from __future__ import annotations

import json

import pytest


def _report_payload() -> dict:
    return {
        "kind": "advertising_report",
        "marketplace": "US",
        "ad_product": "sponsored_products",
        "report_type": "search term report",
        "currency": "USD",
        "time_zone": "America/Los_Angeles",
        "time_unit": "summary",
        "attribution_window": "account report definition",
        "attribution_model": "account report definition",
        "sales_scope": "promoted product attributed sales",
        "window_start": "2026-06-01",
        "window_end": "2026-06-07",
        "metrics": {
            "impressions": 1000,
            "clicks": 50,
            "spend": 25,
            "attributed_orders": 5,
            "attributed_sales": 100,
            "attributed_units": 6,
        },
    }


def test_report_analysis_calculates_only_defined_ratios():
    from ivyea_agent import ads_evidence

    result = ads_evidence.analyze(_report_payload())
    assert result["ready_for_analysis"] is True
    assert result["evidence_level"] == "account_observation"
    assert result["derived_metrics"] == {
        "ctr": 0.05,
        "ctr_percent": 5.0,
        "cpc": 0.5,
        "cvr": 0.1,
        "cvr_percent": 10.0,
        "acos": 0.25,
        "acos_percent": 25.0,
        "roas": 4.0,
    }
    assert result["official_algorithm_claim_allowed"] is False
    assert "search_term_report_can_be_click_filtered" in " ".join(result["interpretation_warnings"])


def test_zero_denominators_return_null_and_missing_context_blocks_readiness():
    from ivyea_agent import ads_evidence

    payload = _report_payload()
    payload["metrics"] = {"impressions": 0, "clicks": 0, "spend": 0, "attributed_sales": 0}
    payload["time_zone"] = ""
    result = ads_evidence.analyze(payload)
    assert all(value is None for value in result["derived_metrics"].values())
    assert result["ready_for_analysis"] is False
    assert "time_zone" in result["missing_inputs"]


def test_report_analysis_rejects_negative_or_invalid_dates():
    from ivyea_agent import ads_evidence

    payload = _report_payload()
    payload["metrics"]["spend"] = -1
    with pytest.raises(ValueError, match="non-negative"):
        ads_evidence.analyze(payload)
    payload = _report_payload()
    payload["metrics"]["spend"] = float("nan")
    with pytest.raises(ValueError, match="non-negative"):
        ads_evidence.analyze(payload)
    payload = _report_payload()
    payload["window_end"] = "2026-05-01"
    with pytest.raises(ValueError, match="must not precede"):
        ads_evidence.analyze(payload)


def test_traffic_experiment_keeps_account_inference_below_algorithm_claim():
    from ivyea_agent import ads_evidence

    payload = {
        **_report_payload(),
        "kind": "traffic_experiment",
        "hypothesis": "A placement adjustment increases qualified top-of-search traffic.",
        "changed_factors": ["top-of-search placement adjustment"],
        "baseline": {
            "window_start": "2026-06-01",
            "window_end": "2026-06-07",
            "metrics": _report_payload()["metrics"],
        },
        "evaluation": {
            "window_start": "2026-06-08",
            "window_end": "2026-06-14",
            "metrics": {**_report_payload()["metrics"], "clicks": 60, "attributed_orders": 6},
        },
        "confounders": ["inventory stable", "price stable", "no listing edit"],
        "control_group": "matched campaign",
        "randomized": True,
        "data_mature": True,
    }
    result = ads_evidence.analyze(payload)
    assert result["ready_for_analysis"] is True
    assert result["comparable_windows_and_definitions"] is True
    assert result["inference_strength"] == "controlled_directional"
    assert result["deltas"]["clicks"] == 10
    assert result["official_algorithm_claim_allowed"] is False
    assert len(result["alternative_explanations_to_check"]) >= 5


def test_ads_capability_matrix_has_dated_dynamic_boundaries():
    from ivyea_agent import ads_evidence

    data = ads_evidence.capability_matrix()
    assert data["retrieved_at"] == "2026-07-06"
    ids = {row["id"] for row in data["products"]}
    assert {"sponsored_products", "amazon_attribution", "amazon_ads_api"} <= ids
    assert "developer portal" in data["policy"]["dynamic_fields"]


def test_ads_evidence_uses_authorized_redacted_knowledge_pipeline(ivyea_home):
    from ivyea_agent import knowledge_evidence

    payload = {
        **_report_payload(),
        "authorized": True,
        "rights_confirmed": True,
        "profile_id": "PRIVATE-PROFILE-123",
        "campaign_id": "PRIVATE-CAMPAIGN-456",
        "content": "Analyst email: analyst@example.com",
    }
    prepared = knowledge_evidence.prepare(payload)
    diagnostic = prepared["evidence"]["diagnostic"]
    assert diagnostic["ready_for_diagnosis"] is True
    assert diagnostic["official_algorithm_claim_allowed"] is False
    assert prepared["evidence"]["refs"]["profile_ref"].startswith("sha256:")
    assert "PRIVATE-PROFILE-123" not in json.dumps(prepared, ensure_ascii=False)
    assert "analyst@example.com" not in json.dumps(prepared, ensure_ascii=False)
    assert "private_account_evidence_review_required" in prepared["draft"]["warnings"]


def test_ads_experiment_diagnostic_redacts_nested_private_text(ivyea_home):
    from ivyea_agent import knowledge_evidence

    report = _report_payload()
    payload = {
        **report,
        "authorized": True,
        "rights_confirmed": True,
        "kind": "traffic_experiment",
        "hypothesis": "Contact owner@example.com if traffic changes",
        "changed_factors": ["bid"],
        "baseline": {"window_start": "2026-06-01", "window_end": "2026-06-07", "metrics": report["metrics"]},
        "evaluation": {"window_start": "2026-06-08", "window_end": "2026-06-14", "metrics": report["metrics"]},
        "confounders": ["Phone: +1 415 555 1212"],
    }
    prepared = knowledge_evidence.prepare(payload)
    serialized = json.dumps(prepared, ensure_ascii=False)
    assert "owner@example.com" not in serialized
    assert "415 555 1212" not in serialized
    assert prepared["evidence"]["redactions"]["email"] == 1
    assert prepared["evidence"]["redactions"]["phone"] == 1


def test_ads_retrieval_prioritizes_measurement_report_and_algorithm_boundaries(ivyea_home):
    from ivyea_agent import knowledge

    metrics = knowledge.evidence_context("亚马逊广告 ACoS 和 ROAS 的归因销售口径", limit=5)
    assert "amazon_ads.metrics_and_denominators" in metrics["ids"]
    assert "amazon_ads.attribution_boundaries" in metrics["ids"]
    reports = knowledge.evidence_context("搜索词报告展示量为什么和广告活动不一致", limit=5)
    assert "amazon_ads.sponsored_products_reports" in reports["ids"]
    algorithm = knowledge.evidence_context("广告提高以后是不是进入更大的流量池并提高自然排名权重", limit=3)
    assert algorithm["ids"][0] == "governance.traffic_algorithm_evidence"


def test_ads_cli_and_service_surfaces(ivyea_home, tmp_path, capsys):
    from ivyea_agent import service
    from ivyea_agent.cli import main

    path = tmp_path / "ads-report.json"
    path.write_text(json.dumps(_report_payload()), encoding="utf-8")
    assert main(["knowledge", "ads-analyze", str(path)]) == 0
    out = capsys.readouterr().out
    assert "official_algorithm_claim_allowed=false" in out
    assert '"acos_percent": 25.0' in out
    assert main(["knowledge", "ads-capabilities"]) == 0
    assert "amazon_ads_api" in capsys.readouterr().out

    analyzed = service.knowledge_ads_analyze(_report_payload())
    assert analyzed["analysis"]["derived_metrics"]["roas"] == 4.0
    assert analyzed["raw_preserved"] is False
    manifest = service.manifest()
    assert manifest["capabilities"]["amazon_ads_evidence_analysis"] is True
    assert any(row["path"] == "/v1/knowledge/ads/analyze" for row in manifest["endpoints"])
