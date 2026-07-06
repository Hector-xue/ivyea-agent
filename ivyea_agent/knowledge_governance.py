"""Knowledge governance dashboard for coverage, freshness, reviews, and conflicts."""
from __future__ import annotations

import time
from typing import Any

from . import knowledge, knowledge_sync


EU_MARKETS = {"DE", "FR", "IT", "ES", "NL", "SE", "PL", "BE", "IE"}
DOMAIN_RULES = {
    "seller_registration": {"categories": {"seller_registration"}},
    "listing_errors": {"categories": {"listing_errors"}},
    "account_health": {"categories": {"account_health"}},
    "policies": {"categories": {"policies"}},
    "seller_fees": {"categories": {"seller_fees"}},
    "restricted_products": {"categories": {"restricted_products"}},
    "dangerous_goods": {"categories": {"dangerous_goods"}},
    "listing_requirements": {"categories": {"listing_requirements"}},
    "amazon_ads": {"categories": {"amazon_ads", "ads_experimentation"}},
    "ads_measurement": {"categories": {"ads_measurement"}},
    "ads_reporting": {"categories": {"ads_reporting"}},
    "traffic_governance": {"categories": {"traffic_governance"}},
    "tax_compliance": {"categories": {"tax_compliance"}},
    "finance_settlement": {"categories": {"finance_settlement"}},
    "returns_claims": {"categories": {"returns_claims"}},
    "brand_registry": {"categories": {"brand_registry"}},
    "brand_protection": {"categories": {"brand_protection"}},
    "sponsored_brands": {"categories": {"sponsored_brands"}},
    "display_ads": {"categories": {"display_ads"}},
    "amazon_dsp": {"categories": {"amazon_dsp"}},
    "ads_clean_room": {"categories": {"ads_clean_room"}},
}
CRITICAL_REQUIREMENTS = (
    ("seller_registration", "US"),
    ("seller_registration", "UK"),
    ("seller_registration", "EU"),
    ("seller_registration", "JP"),
    ("seller_registration", "CA"),
    ("seller_registration", "AU"),
    ("seller_registration", "SG"),
    ("seller_registration", "IN"),
    ("seller_registration", "AE"),
    ("listing_errors", "GLOBAL"),
    ("account_health", "US"),
    ("account_health", "UK"),
    ("account_health", "EU"),
    ("account_health", "JP"),
    ("policies", "US"),
    ("seller_fees", "US"),
    ("seller_fees", "UK"),
    ("seller_fees", "EU"),
    ("seller_fees", "JP"),
    ("restricted_products", "US"),
    ("restricted_products", "UK"),
    ("restricted_products", "EU"),
    ("restricted_products", "JP"),
    ("dangerous_goods", "US"),
    ("dangerous_goods", "UK"),
    ("dangerous_goods", "EU"),
    ("dangerous_goods", "JP"),
    ("listing_requirements", "GLOBAL"),
    ("amazon_ads", "GLOBAL"),
    ("ads_measurement", "GLOBAL"),
    ("ads_reporting", "GLOBAL"),
    ("traffic_governance", "GLOBAL"),
    ("tax_compliance", "GLOBAL"),
    ("finance_settlement", "GLOBAL"),
    ("returns_claims", "GLOBAL"),
    ("brand_registry", "GLOBAL"),
    ("brand_protection", "GLOBAL"),
    ("sponsored_brands", "GLOBAL"),
    ("display_ads", "GLOBAL"),
    ("amazon_dsp", "GLOBAL"),
    ("ads_clean_room", "GLOBAL"),
)


def _market_applies(card_markets: list[str], target: str, *, allow_global: bool = True) -> bool:
    markets = {str(value).upper() for value in card_markets}
    if target == "GLOBAL":
        return "GLOBAL" in markets
    if allow_global and "GLOBAL" in markets:
        return True
    if target == "EU":
        return bool(markets & EU_MARKETS)
    return target in markets


def _cell_status(cards: list[dict[str, Any]]) -> str:
    if not cards:
        return "gap"
    primary = [card for card in cards if str(card.get("authority_tier") or "").startswith("primary")]
    if any(card.get("freshness") == "current" for card in primary):
        return "strong"
    if primary:
        return "review_due"
    if any(card.get("authority_tier") == "internal_governance" for card in cards):
        return "governed"
    return "synthesis_only"


def coverage() -> dict[str, Any]:
    cards = knowledge.list_builtin_cards()
    requirements = []
    for domain, marketplace in CRITICAL_REQUIREMENTS:
        categories = DOMAIN_RULES[domain]["categories"]
        explicitly_local = domain in {
            "seller_registration", "account_health", "policies", "seller_fees",
            "restricted_products", "dangerous_goods",
        }
        matched = [
            card for card in cards
            if card.get("category") in categories and _market_applies(
                list(card.get("marketplaces") or []), marketplace, allow_global=not explicitly_local,
            )
        ]
        status = _cell_status(matched)
        requirements.append({
            "domain": domain,
            "marketplace": marketplace,
            "status": status,
            "covered": status != "gap",
            "primary_current": status == "strong",
            "card_ids": [str(card.get("id")) for card in matched],
            "source_urls": sorted({str(card.get("source_url")) for card in matched if card.get("source_url")}),
        })
    covered = sum(1 for row in requirements if row["covered"])
    strong = sum(1 for row in requirements if row["primary_current"])
    return {
        "summary": {
            "requirements": len(requirements),
            "covered": covered,
            "gaps": len(requirements) - covered,
            "primary_current": strong,
            "coverage_rate": covered / len(requirements) if requirements else 0.0,
            "primary_current_rate": strong / len(requirements) if requirements else 0.0,
        },
        "requirements": requirements,
        "policy": (
            "Registration, account health, policy, fee, restricted-product, and dangerous-goods coverage requires an "
            "explicit marketplace; GLOBAL applies only to genuinely cross-market technical, reporting, brand-program, "
            "or advertising domains."
        ),
    }


def freshness(now: float | None = None) -> dict[str, Any]:
    epoch = float(now if now is not None else time.time())
    cards = knowledge.audit()["cards"]
    card_counts = {
        level: sum(1 for card in cards if card.get("freshness") == level)
        for level in ("current", "aging_review_soon", "stale_needs_review", "undated", "reviewed")
    }
    state = knowledge_sync.status()["state"].get("sources") or {}
    source_rows = []
    for source in knowledge_sync.load_registry():
        if not source.get("enabled") or source.get("requires_auth") or source.get("update_mode") not in knowledge_sync.FETCHABLE_MODES:
            continue
        observed = state.get(source["id"]) or {}
        checked_epoch = float(observed.get("last_checked_epoch") or 0)
        overdue = bool(checked_epoch and epoch - checked_epoch > float(source["cadence_hours"]) * 3600 * 1.5)
        if observed.get("last_status") == "error":
            status = "error"
        elif not checked_epoch:
            status = "unseen"
        elif overdue:
            status = "overdue"
        else:
            status = "current"
        source_rows.append({
            "id": source["id"],
            "status": status,
            "cadence_hours": source["cadence_hours"],
            "last_checked": observed.get("last_checked", ""),
            "last_change": observed.get("last_change", ""),
            "last_error": observed.get("last_error", ""),
        })
    source_counts = {name: sum(1 for row in source_rows if row["status"] == name) for name in ("current", "unseen", "overdue", "error")}
    return {
        "summary": {
            "cards": len(cards),
            "card_freshness": card_counts,
            "monitor_sources": len(source_rows),
            "monitor_status": source_counts,
        },
        "cards_requiring_review": [
            card for card in cards if card.get("freshness") in {"aging_review_soon", "stale_needs_review", "undated"}
        ],
        "sources": source_rows,
    }


def dashboard(now: float | None = None) -> dict[str, Any]:
    queue = knowledge_sync.changes(limit=10)
    cover = coverage()
    fresh = freshness(now=now)
    conflicts = knowledge.conflicts()
    stale = fresh["summary"]["card_freshness"]["stale_needs_review"]
    monitor = fresh["summary"]["monitor_status"]
    unverified_approved = int(queue["summary"].get("unverified_approved") or 0)
    healthy = not any((
        queue["summary"]["pending"], cover["summary"]["gaps"], stale,
        monitor["error"], len(conflicts), unverified_approved,
    ))
    return {
        "ok": True,
        "healthy": healthy,
        "summary": {
            "pending_reviews": queue["summary"]["pending"],
            "approved_not_published": queue["summary"].get("approved_not_published", queue["summary"]["approved"]),
            "published_changes": queue["summary"].get("published", 0),
            "coverage_gaps": cover["summary"]["gaps"],
            "stale_cards": stale,
            "monitor_errors": monitor["error"],
            "monitor_overdue": monitor["overdue"],
            "conflicts": len(conflicts),
            "unverified_approved": unverified_approved,
        },
        "reviews": queue,
        "coverage": cover,
        "freshness": fresh,
        "conflicts": conflicts,
    }


def render_dashboard(data: dict[str, Any] | None = None) -> str:
    data = data or dashboard()
    summary = data["summary"]
    lines = [
        "Ivyea Amazon 知识治理看板：",
        f"- healthy={str(bool(data['healthy'])).lower()} pending_reviews={summary['pending_reviews']} "
        f"approved_not_published={summary['approved_not_published']}",
        f"- coverage_gaps={summary['coverage_gaps']} stale_cards={summary['stale_cards']} "
        f"monitor_errors={summary['monitor_errors']} monitor_overdue={summary['monitor_overdue']} "
        f"conflicts={summary['conflicts']} unverified_approved={summary['unverified_approved']}",
    ]
    for row in data["coverage"]["requirements"]:
        lines.append(
            f"- {row['domain']}@{row['marketplace']} | {row['status']} | "
            f"cards={','.join(row['card_ids']) or '-'}"
        )
    return "\n".join(lines)


def render_coverage(data: dict[str, Any] | None = None) -> str:
    data = data or coverage()
    summary = data["summary"]
    lines = [
        "Ivyea Amazon 知识覆盖矩阵：",
        f"- requirements={summary['requirements']} covered={summary['covered']} gaps={summary['gaps']} "
        f"coverage_rate={summary['coverage_rate']:.1%} primary_current={summary['primary_current_rate']:.1%}",
    ]
    for row in data["requirements"]:
        lines.append(
            f"- {row['domain']}@{row['marketplace']} | {row['status']} | "
            f"cards={','.join(row['card_ids']) or '-'}"
        )
    return "\n".join(lines)


def render_freshness(data: dict[str, Any] | None = None) -> str:
    data = data or freshness()
    summary = data["summary"]
    card_counts = summary["card_freshness"]
    monitor = summary["monitor_status"]
    lines = [
        "Ivyea Amazon 知识时效看板：",
        f"- cards={summary['cards']} current={card_counts['current']} aging={card_counts['aging_review_soon']} "
        f"stale={card_counts['stale_needs_review']} undated={card_counts['undated']}",
        f"- monitor_sources={summary['monitor_sources']} current={monitor['current']} unseen={monitor['unseen']} "
        f"overdue={monitor['overdue']} error={monitor['error']}",
    ]
    for row in data["cards_requiring_review"]:
        lines.append(f"- card {row['id']} | {row['freshness']} | retrieved={row['retrieved_at'] or '-'}")
    for row in data["sources"]:
        if row["status"] in {"overdue", "error"}:
            lines.append(f"- source {row['id']} | {row['status']} | last_checked={row['last_checked'] or '-'}")
    return "\n".join(lines)
