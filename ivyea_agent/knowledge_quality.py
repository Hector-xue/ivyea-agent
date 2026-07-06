"""Data-driven, deterministic quality checks for Amazon knowledge retrieval."""
from __future__ import annotations

import json
from importlib import resources
from typing import Any

from . import knowledge


def cases() -> list[dict[str, Any]]:
    path = resources.files("ivyea_agent").joinpath("knowledge_base/knowledge_quality_cases.json")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("knowledge quality cases must be a list")
    return rows


def run() -> dict[str, Any]:
    results = []
    for case in cases():
        evidence = knowledge.evidence_context(str(case["query"]), limit=int(case.get("limit") or 5))
        ids = evidence.get("ids") or []
        expected = [str(value) for value in case.get("expected_ids") or []]
        max_rank = int(case.get("max_rank") or len(ids) or 1)
        matched_ranks = {card_id: (ids.index(card_id) + 1 if card_id in ids else None) for card_id in expected}
        recall_ok = all(rank is not None and rank <= max_rank for rank in matched_ranks.values())
        risk_ok = not case.get("risk") or evidence.get("risk") == case.get("risk")
        citation = knowledge.validate_citations("Evidence-bounded statement. [K1]", evidence.get("citations") or [])
        citation_ok = bool(evidence.get("citations")) and citation["ok"]
        first = (evidence.get("citations") or [{}])[0]
        authority_ok = not case.get("first_authority") or first.get("authority_tier") == case.get("first_authority")
        evidence_class_ok = not case.get("first_evidence_class") or first.get("evidence_class") == case.get("first_evidence_class")
        ok = recall_ok and risk_ok and citation_ok and authority_ok and evidence_class_ok
        results.append({
            "id": case["id"],
            "domain": case["domain"],
            "ok": ok,
            "query": case["query"],
            "ids": ids,
            "matched_ranks": matched_ranks,
            "risk": evidence.get("risk"),
            "checks": {
                "recall": recall_ok,
                "risk": risk_ok,
                "citation": citation_ok,
                "authority": authority_ok,
                "evidence_class": evidence_class_ok,
            },
        })
    passed = sum(1 for row in results if row["ok"])
    domains = sorted({str(row["domain"]) for row in results})
    domain_summary = {
        domain: {
            "cases": sum(1 for row in results if row["domain"] == domain),
            "passed": sum(1 for row in results if row["domain"] == domain and row["ok"]),
        }
        for domain in domains
    }
    return {
        "ok": passed == len(results),
        "summary": {
            "cases": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": passed / len(results) if results else 0.0,
            "domains": domain_summary,
        },
        "results": results,
    }


def render(result: dict[str, Any] | None = None) -> str:
    result = result or run()
    summary = result["summary"]
    lines = [
        "Ivyea Amazon 知识质量评测：",
        f"- result={'PASS' if result['ok'] else 'FAIL'} cases={summary['cases']} "
        f"passed={summary['passed']} failed={summary['failed']} pass_rate={summary['pass_rate']:.1%}",
    ]
    for row in result["results"]:
        ranks = ",".join(f"{key}:{value or '-'}" for key, value in row["matched_ranks"].items())
        lines.append(f"- {'PASS' if row['ok'] else 'FAIL'} {row['id']} | risk={row['risk']} | ranks={ranks}")
    return "\n".join(lines)
