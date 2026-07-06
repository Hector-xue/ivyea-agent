"""Lightweight product evals for Ivyea Agent."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from . import (
    ads_evidence, alerts, competitor_audit, image_audit, knowledge, knowledge_evidence,
    knowledge_governance, knowledge_quality, knowledge_sync, listing_audit, mcp_source, notify, ocr,
    offer_audit, policy, review_audit, rule_engine, schedule, security, skills, vision, weekly_review,
)


def run() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    out = rule_engine.run(str(rule_engine.SAMPLE_CSV), site="US", output_dir=tempfile.mkdtemp())
    summary = out["summary"]
    golden = {
        "asin": "B0PUBLIC01",
        "negative_candidate_count": 1,
        "scale_up_count": 1,
        "reduce_bid_count": 1,
    }
    for key, expected in golden.items():
        actual = summary.get(key)
        checks.append({
            "name": f"rule_engine.{key}",
            "ok": actual == expected,
            "detail": f"expected={expected} actual={actual}",
        })

    khits = knowledge.search("否词 高点击 零单", limit=5)
    checks.append({
        "name": "knowledge.negative_recall",
        "ok": any(h["id"] == "playbook.negative_keyword_risk" for h in khits),
        "detail": ",".join(h["id"] for h in khits),
    })
    registration = knowledge.evidence_context("亚马逊卖家注册身份验证失败怎么办", limit=3)
    checks.append({
        "name": "knowledge.registration_official_recall",
        "ok": bool(registration["citations"]) and (
            registration["citations"][0]["id"] == "seller_registration.registration_and_identity_verification"
            and registration["citations"][0]["authority_tier"] == "primary"
        ),
        "detail": ",".join(registration.get("ids") or []),
    })
    listing_error = knowledge.evidence_context("上架报错 90220", limit=3)
    citation_check = knowledge.validate_citations("先核对当前 schema。[K1]", listing_error["citations"])
    checks.append({
        "name": "knowledge.listing_error_citation",
        "ok": bool(listing_error["citations"]) and (
            listing_error["citations"][0]["id"] == "seller_central.listings_items_error_diagnostics"
            and citation_check["ok"]
        ),
        "detail": f"risk={listing_error['risk']} ids={','.join(listing_error.get('ids') or [])}",
    })
    source_audit = knowledge.source_registry()["summary"]
    checks.append({
        "name": "knowledge.source_traceability",
        "ok": source_audit["missing_source_url_cards"] == 0 and source_audit["official_sources"] >= 1,
        "detail": f"missing_urls={source_audit['missing_source_url_cards']} sources={source_audit['sources']}",
    })
    phase_two_cases = [
        ("日本站注册身份验证", "seller_registration.japan_registration"),
        ("账户停用绩效通知申诉", "policies.account_health_appeal_evidence"),
        ("费用佣金估算和实际不一致", "fees.selling_fees_and_estimates"),
        ("受限商品危险品 SDS", "policies.restricted_products_diagnostics"),
        ("GTIN 变体父子体报错", "listing.gtin_and_exemptions"),
    ]
    missed = []
    for query, expected in phase_two_cases:
        ids = knowledge.evidence_context(query, limit=5).get("ids") or []
        if expected not in ids:
            missed.append(f"{expected}:{','.join(ids)}")
    checks.append({
        "name": "knowledge.high_risk_domain_recall",
        "ok": not missed,
        "detail": "all high-risk cases recalled" if not missed else ";".join(missed),
    })
    official_registry = knowledge_sync.registry()["summary"]
    checks.append({
        "name": "knowledge.official_source_coverage",
        "ok": official_registry["sources"] >= 25 and official_registry["authorization_required"] >= 5,
        "detail": (
            f"sources={official_registry['sources']} public={official_registry['public_monitorable']} "
            f"auth={official_registry['authorization_required']}"
        ),
    })
    private_text, redaction_counts = knowledge_evidence.redact_evidence_text(
        "Email: owner@example.com\nPhone: +1 415 555 1212\nPassport number: P12345678"
    )
    checks.append({
        "name": "knowledge.authorized_evidence_redaction",
        "ok": "owner@example.com" not in private_text and "P12345678" not in private_text and bool(redaction_counts),
        "detail": json.dumps(redaction_counts, ensure_ascii=False, sort_keys=True),
    })
    ads_cases = [
        ("广告 ACoS ROAS 归因销售口径", "amazon_ads.metrics_and_denominators"),
        ("搜索词报告展示量不一致", "amazon_ads.sponsored_products_reports"),
        ("流量池算法权重自然排名", "governance.traffic_algorithm_evidence"),
    ]
    ads_missed = []
    for query, expected in ads_cases:
        ids = knowledge.evidence_context(query, limit=5).get("ids") or []
        if expected not in ids:
            ads_missed.append(f"{expected}:{','.join(ids)}")
    checks.append({
        "name": "knowledge.ads_evidence_recall",
        "ok": not ads_missed,
        "detail": "ads evidence boundaries recalled" if not ads_missed else ";".join(ads_missed),
    })
    ads_report = ads_evidence.analyze({
        "kind": "advertising_report", "ad_product": "sponsored_products",
        "report_type": "search term report", "currency": "USD", "time_zone": "UTC",
        "window_start": "2026-06-01", "window_end": "2026-06-07",
        "metrics": {"impressions": 100, "clicks": 10, "spend": 5, "attributed_sales": 20},
    })
    checks.append({
        "name": "knowledge.ads_metric_and_inference_gate",
        "ok": ads_report["derived_metrics"]["acos"] == 0.25 and not ads_report["official_algorithm_claim_allowed"],
        "detail": f"acos={ads_report['derived_metrics']['acos']} algorithm_claim={ads_report['official_algorithm_claim_allowed']}",
    })
    quality = knowledge_quality.run()
    checks.append({
        "name": "knowledge.continuous_quality_suite",
        "ok": bool(quality["ok"]) and quality["summary"]["cases"] >= 15,
        "detail": (
            f"cases={quality['summary']['cases']} passed={quality['summary']['passed']} "
            f"rate={quality['summary']['pass_rate']:.1%}"
        ),
    })
    coverage = knowledge_governance.coverage()
    checks.append({
        "name": "knowledge.coverage_matrix_complete",
        "ok": (
            coverage["summary"]["requirements"] >= 41
            and coverage["summary"]["covered"] == coverage["summary"]["requirements"]
            and coverage["summary"]["gaps"] == 0
        ),
        "detail": (
            f"requirements={coverage['summary']['requirements']} covered={coverage['summary']['covered']} "
            f"gaps={coverage['summary']['gaps']}"
        ),
    })
    kidx = knowledge.rebuild_index()
    checks.append({
        "name": "knowledge.index_shape",
        "ok": kidx["cards"] >= 10 and "db" in kidx,
        "detail": f"cards={kidx['cards']} fts={kidx['fts']}",
    })

    shits = skills.search("listing 转化 主图", limit=5)
    checks.append({
        "name": "skill.listing_recall",
        "ok": any(sk.id == "amazon.listing_conversion_audit" for sk, _ in shits),
        "detail": ",".join(sk.id for sk, _ in shits),
    })

    redacted = security.redact_text("api_key=sk-test1234567890abcdef")
    checks.append({
        "name": "security.redaction",
        "ok": "sk-test" not in redacted and "***REDACTED***" in redacted,
        "detail": redacted,
    })

    la = listing_audit.audit(
        title="Karaoke Microphone",
        bullets="Bluetooth speaker for party",
        search_terms=["karaoke microphone kids", "recording studio mic"],
        reviews="does not work",
        rating=3.7,
        review_count=5,
    )
    checks.append({
        "name": "listing.intent_and_review_risk",
        "ok": bool(la["gaps"]) and any(r["area"] == "reviews" and r["level"] == "high" for r in la["risks"]),
        "detail": f"gaps={len(la['gaps'])} risks={len(la['risks'])}",
    })

    ra = review_audit.audit(
        reviews="Poor quality, stopped working, missing parts",
        rating=3.8,
        review_count=8,
        price=39.99,
        competitor_price=29.99,
    )
    checks.append({
        "name": "review.offer_trust_risk",
        "ok": bool(ra["issues"]) and any(r["area"] == "price" for r in ra["risks"]),
        "detail": f"issues={len(ra['issues'])} risks={len(ra['risks'])}",
    })

    oa = offer_audit.audit(margin_rate=0.30, target_acos=0.36, inventory_days=8, spend=120, sales=250)
    checks.append({
        "name": "offer.margin_inventory_guard",
        "ok": any(r["area"] == "inventory" for r in oa["risks"]) and any(r["area"] == "margin" for r in oa["risks"]),
        "detail": f"risks={len(oa['risks'])} guidance={oa['ad_guidance'][:40]}",
    })

    ca = competitor_audit.audit(
        own_terms="karaoke machine,kids microphone",
        search_terms="acme karaoke,B0ABCDEFGH,wireless microphone",
        competitor_terms="acme",
        category_terms="microphone,karaoke",
    )
    checks.append({
        "name": "competitor.keyword_structure",
        "ok": bool(ca["competitor_hits"]) and bool(ca["asin_hits"]) and bool(ca["missing_core"]),
        "detail": f"competitor={len(ca['competitor_hits'])} asin={len(ca['asin_hits'])} missing={len(ca['missing_core'])}",
    })

    wr = weekly_review.render(weekly_review.build(limit=20))
    checks.append({
        "name": "weekly.review_shape",
        "ok": "本周期优先事项" in wr and "下一步建议" in wr,
        "detail": "shape ok" if "本周期优先事项" in wr else "missing sections",
    })

    ar = alerts.render(alerts.check(limit=20))
    ok, sr = schedule.run_task("alert", {"limit": 20})
    checks.append({
        "name": "alerts.schedule_shape",
        "ok": "Ivyea Alerts" in ar and ok and "Ivyea Alerts" in sr,
        "detail": "shape ok" if "Ivyea Alerts" in ar else "missing alert header",
    })

    nr = notify.send("api_key=sk-test1234567890abcdef", title="Eval", channel="stdout")
    checks.append({
        "name": "notify.stdout_redaction",
        "ok": bool(nr["ok"]) and "sk-test" not in nr["message"] and "***REDACTED***" in nr["message"],
        "detail": "stdout redacted" if nr["ok"] else nr.get("error", ""),
    })

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "main.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (800).to_bytes(4, "big") + (600).to_bytes(4, "big") + b"\x00" * 20)
        ia = image_audit.audit([td])
    checks.append({
        "name": "image.asset_shape",
        "ok": bool(ia["images"]) and any(r["area"] == "resolution" for r in ia["risks"]),
        "detail": f"images={len(ia['images'])} risks={len(ia['risks'])}",
    })

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "main.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1200).to_bytes(4, "big") + (1200).to_bytes(4, "big") + b"\x00" * 20)
        vp = vision.build("openai", [td], product_context="karaoke", max_images=1)
    checks.append({
        "name": "vision.package_shape",
        "ok": vp["provider"] == "openai" and bool(vp["images"]) and "payload" in vp,
        "detail": f"provider={vp['provider']} images={len(vp['images'])}",
    })

    avail, detail = ocr.available()
    checks.append({
        "name": "ocr.adapter_shape",
        "ok": isinstance(avail, bool) and bool(detail),
        "detail": detail,
    })

    cmd_ok, cmd_msg = policy.check_command("git reset --hard")
    checks.append({
        "name": "policy.command_guard",
        "ok": not cmd_ok and "拒绝" in cmd_msg,
        "detail": cmd_msg,
    })

    suggestion = mcp_source.suggest_data_source("get_report", {}, {
        "structuredContent": {
            "data": {"rows": [{"asin": "B0X", "searchTerm": "karaoke", "clicks": 8, "spend": 4.2}]}
        }
    })
    checks.append({
        "name": "mcp.datasource_suggestion",
        "ok": (
            suggestion["dataSource"]["rows_path"] == "data.rows"
            and suggestion["dataSource"]["field_map"].get("Customer Search Term") == "searchTerm"
        ),
        "detail": f"mapped={suggestion['coverage']['mapped']} rows_path={suggestion['dataSource']['rows_path']}",
    })

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
    }


def render(result: dict[str, Any]) -> str:
    lines = ["Ivyea Agent Eval", ""]
    for c in result["checks"]:
        lines.append(f"- {'PASS' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")
    lines.append("")
    lines.append("result: " + ("PASS" if result["ok"] else "FAIL"))
    return "\n".join(lines)
