"""Deterministic Amazon Ads report and traffic-experiment evidence analysis.

This module calculates only transparent ratios from user-supplied account data.
It never treats attributed sales, rank movement, or a before/after comparison as
proof of Amazon's auction or organic-ranking algorithm.
"""
from __future__ import annotations

import json
import math
from datetime import date
from importlib import resources
from typing import Any


AD_PRODUCTS = {
    "sponsored_products",
    "sponsored_brands",
    "sponsored_display",
    "dsp",
    "amazon_attribution",
    "other",
}
METRIC_FIELDS = (
    "impressions",
    "clicks",
    "spend",
    "attributed_orders",
    "attributed_sales",
    "attributed_units",
)
ALTERNATIVE_EXPLANATIONS = (
    "bid, budget, bidding strategy, or placement-mix changes",
    "inventory, Featured Offer eligibility, price, coupon, or delivery-promise changes",
    "rating, reviews, detail-page content, variation, or retail-readiness changes",
    "seasonality, promotions, competitor auctions, or category-demand changes",
    "overlapping campaigns, off-Amazon placements, or audience-mix changes",
    "attribution lag, invalid-traffic adjustment, cancellation, or report restatement",
)


def capability_matrix() -> dict[str, Any]:
    path = resources.files("ivyea_agent").joinpath("knowledge_base/amazon_ads_capabilities.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _number(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return number


def normalize_metrics(raw: Any) -> dict[str, float | None]:
    if not isinstance(raw, dict):
        return {field: None for field in METRIC_FIELDS}
    return {field: _number(raw.get(field), f"metrics.{field}") for field in METRIC_FIELDS}


def derived_metrics(metrics: dict[str, float | None]) -> dict[str, float | None]:
    impressions = metrics.get("impressions")
    clicks = metrics.get("clicks")
    spend = metrics.get("spend")
    orders = metrics.get("attributed_orders")
    sales = metrics.get("attributed_sales")
    return {
        "ctr": clicks / impressions if clicks is not None and impressions else None,
        "ctr_percent": clicks * 100 / impressions if clicks is not None and impressions else None,
        "cpc": spend / clicks if spend is not None and clicks else None,
        "cvr": orders / clicks if orders is not None and clicks else None,
        "cvr_percent": orders * 100 / clicks if orders is not None and clicks else None,
        "acos": spend / sales if spend is not None and sales else None,
        "acos_percent": spend * 100 / sales if spend is not None and sales else None,
        "roas": sales / spend if sales is not None and spend else None,
    }


def _iso_date(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc
    return text


def _window(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    key = f"{prefix}_" if prefix else ""
    start = _iso_date(payload.get(f"{key}window_start"), f"{key}window_start")
    end = _iso_date(payload.get(f"{key}window_end"), f"{key}window_end")
    days = None
    if start and end:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        if end_date < start_date:
            raise ValueError(f"{key}window_end must not precede {key}window_start")
        days = (end_date - start_date).days + 1
    return {"start": start, "end": end, "days": days}


def _common(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    product = str(payload.get("ad_product") or "").lower().strip()
    if product and product not in AD_PRODUCTS:
        raise ValueError("ad_product must be one of: " + ", ".join(sorted(AD_PRODUCTS)))
    common = {
        "ad_product": product,
        "report_type": str(payload.get("report_type") or "").strip()[:120],
        "currency": str(payload.get("currency") or "").upper().strip()[:8],
        "time_zone": str(payload.get("time_zone") or "").strip()[:80],
        "time_unit": str(payload.get("time_unit") or "").lower().strip()[:30],
        "attribution_window": str(payload.get("attribution_window") or "").strip()[:80],
        "attribution_model": str(payload.get("attribution_model") or "").strip()[:120],
        "sales_scope": str(payload.get("sales_scope") or "").strip()[:160],
    }
    required = ["ad_product", "report_type", "currency", "time_zone"]
    missing = [field for field in required if not common[field]]
    return common, missing


def analyze_report(payload: dict[str, Any]) -> dict[str, Any]:
    common, missing = _common(payload)
    window = _window(payload)
    if not window["start"]:
        missing.append("window_start")
    if not window["end"]:
        missing.append("window_end")
    metrics = normalize_metrics(payload.get("metrics"))
    if not any(value is not None for value in metrics.values()):
        missing.append("metrics")
    interpretation_warnings = []
    for field in ("attribution_window", "attribution_model", "sales_scope"):
        if not common[field]:
            interpretation_warnings.append(f"{field}_not_recorded")
    if common["report_type"].lower() in {"search term", "search_term", "search term report"}:
        interpretation_warnings.append("search_term_report_can_be_click_filtered_and_include_inferred_or_asin_terms")
    return {
        "evidence_level": "account_observation",
        "ready_for_analysis": not missing,
        "missing_inputs": sorted(set(missing)),
        "context": common,
        "window": window,
        "metrics": metrics,
        "derived_metrics": derived_metrics(metrics),
        "interpretation_warnings": interpretation_warnings,
        "official_algorithm_claim_allowed": False,
        "reasoning_boundary": (
            "Calculated ratios describe the supplied report only. Attributed conversions are not proof of "
            "incrementality, and account observations are not Amazon auction or organic-ranking rules."
        ),
    }


def _period(raw: Any, name: str) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(raw, dict):
        return {"window": {"start": "", "end": "", "days": None}, "metrics": {}, "derived_metrics": {}}, [name]
    window = _window(raw)
    metrics = normalize_metrics(raw.get("metrics"))
    missing = []
    if not window["start"]:
        missing.append(f"{name}.window_start")
    if not window["end"]:
        missing.append(f"{name}.window_end")
    if not any(value is not None for value in metrics.values()):
        missing.append(f"{name}.metrics")
    return {"window": window, "metrics": metrics, "derived_metrics": derived_metrics(metrics)}, missing


def _deltas(baseline: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, float | None]:
    fields = (*METRIC_FIELDS, "ctr", "cpc", "cvr", "acos", "roas")
    result: dict[str, float | None] = {}
    for field in fields:
        left = baseline["metrics"].get(field) if field in METRIC_FIELDS else baseline["derived_metrics"].get(field)
        right = evaluation["metrics"].get(field) if field in METRIC_FIELDS else evaluation["derived_metrics"].get(field)
        result[field] = right - left if left is not None and right is not None else None
    return result


def analyze_experiment(payload: dict[str, Any]) -> dict[str, Any]:
    common, missing = _common(payload)
    hypothesis = str(payload.get("hypothesis") or "").strip()[:500]
    if not hypothesis:
        missing.append("hypothesis")
    changed_factors = payload.get("changed_factors")
    if isinstance(changed_factors, str):
        changed_factors = [changed_factors]
    if not isinstance(changed_factors, list):
        changed_factors = []
    changed_factors = [str(value).strip()[:120] for value in changed_factors if str(value).strip()]
    if not changed_factors:
        missing.append("changed_factors")
    baseline, baseline_missing = _period(payload.get("baseline"), "baseline")
    evaluation, evaluation_missing = _period(payload.get("evaluation"), "evaluation")
    missing.extend(baseline_missing + evaluation_missing)
    confounders = payload.get("confounders")
    if isinstance(confounders, str):
        confounders = [confounders]
    if not isinstance(confounders, list):
        confounders = []
    confounders = [str(value).strip()[:240] for value in confounders if str(value).strip()]
    randomized = payload.get("randomized") is True
    control_group = str(payload.get("control_group") or "").strip()[:240]
    data_mature = payload.get("data_mature") is True
    comparable = bool(
        baseline["window"]["days"]
        and baseline["window"]["days"] == evaluation["window"]["days"]
        and common["currency"]
        and common["attribution_window"]
        and common["attribution_model"]
        and common["sales_scope"]
    )
    design_warnings = []
    if len(changed_factors) != 1:
        design_warnings.append("multiple_changes_prevent_effect_isolation")
    if baseline["window"]["days"] != evaluation["window"]["days"]:
        design_warnings.append("unequal_window_lengths")
    if not data_mature:
        design_warnings.append("attribution_or_report_restatement_may_be_incomplete")
    if not confounders:
        design_warnings.append("confounders_not_recorded")
    if not common["attribution_window"] or not common["attribution_model"] or not common["sales_scope"]:
        design_warnings.append("attribution_definition_incomplete")
    if randomized and not control_group:
        design_warnings.append("randomized_flag_without_control_group")
    strength = "controlled_directional" if randomized and control_group and comparable and data_mature else "observational_directional"
    return {
        "evidence_level": "account_inference",
        "ready_for_analysis": not missing,
        "missing_inputs": sorted(set(missing)),
        "hypothesis": hypothesis,
        "changed_factors": changed_factors,
        "context": common,
        "baseline": baseline,
        "evaluation": evaluation,
        "deltas": _deltas(baseline, evaluation) if not (baseline_missing or evaluation_missing) else {},
        "comparable_windows_and_definitions": comparable,
        "inference_strength": strength,
        "design_warnings": design_warnings,
        "recorded_confounders": confounders,
        "alternative_explanations_to_check": list(ALTERNATIVE_EXPLANATIONS),
        "official_algorithm_claim_allowed": False,
        "reasoning_boundary": (
            "A before/after or controlled account experiment can support an account-specific inference. "
            "It cannot establish an undocumented Amazon auction, traffic-pool, weighting, or organic-ranking rule."
        ),
    }


def analyze(payload: dict[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or "").strip()
    if kind == "advertising_report":
        return analyze_report(payload)
    if kind == "traffic_experiment":
        return analyze_experiment(payload)
    raise ValueError("ads evidence kind must be advertising_report or traffic_experiment")


def render_analysis(result: dict[str, Any]) -> str:
    lines = [
        "Amazon Ads 证据分析：",
        f"- evidence_level={result.get('evidence_level')}",
        f"- ready_for_analysis={str(bool(result.get('ready_for_analysis'))).lower()}",
        f"- missing_inputs={','.join(result.get('missing_inputs') or []) or '-'}",
        f"- official_algorithm_claim_allowed={str(bool(result.get('official_algorithm_claim_allowed'))).lower()}",
    ]
    if result.get("inference_strength"):
        lines.append(f"- inference_strength={result['inference_strength']}")
    lines.append("- boundary=" + str(result.get("reasoning_boundary") or ""))
    return "\n".join(lines)
