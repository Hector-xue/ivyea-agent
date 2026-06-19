"""Lightweight product evals for Ivyea Agent."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from . import alerts, competitor_audit, image_audit, knowledge, listing_audit, mcp_source, notify, ocr, offer_audit, policy, review_audit, rule_engine, schedule, security, skills, vision, weekly_review


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
