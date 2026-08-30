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


def _covered(text_low: str, group: Any) -> bool:
    """一个黄金要点组里，命中任意一个同义说法就算覆盖。"""
    options = group if isinstance(group, list) else [group]
    return any(str(option).lower() in text_low for option in options if str(option).strip())


def run() -> dict[str, Any]:
    results = []
    for case in cases():
        evidence = knowledge.evidence_context(str(case["query"]), limit=int(case.get("limit") or 5))
        ids = evidence.get("ids") or []
        text_low = str(evidence.get("text") or "").lower()
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

        # —— 答案级断言（确定性层，不调 LLM）——
        # 只测"证据够不够回答这题"，不测措辞。测措辞要真跑模型，那是 answer_evals 那一层。
        missing_points = [
            group for group in (case.get("golden_points") or []) if not _covered(text_low, group)
        ]
        golden_ok = not missing_points
        # 禁止说法出现在**注入证据**里就算失败：说明召回了会把模型带偏的材料。
        hit_forbidden = [
            str(term) for term in (case.get("forbidden") or []) if str(term).lower() in text_low
        ]
        forbidden_ok = not hit_forbidden
        # 幻觉陷阱题：问一个亚马逊没有的机制时，必须把护栏卡一起召回，
        # 否则模型拿着一堆不相干材料，最容易顺着问题把不存在的东西编圆。
        guards = [str(card_id) for card_id in (case.get("expect_guard") or [])]
        guard_ok = not guards or any(card_id in ids for card_id in guards)
        # 真正的知识缺口题：期望走到"没有命中"分支，而不是硬凑证据。
        gap_ok = not case.get("expect_gap") or not (evidence.get("citations") or [])

        ok = (
            recall_ok and risk_ok and citation_ok and authority_ok and evidence_class_ok
            and golden_ok and forbidden_ok and guard_ok and gap_ok
        )
        # known_gap = 已确认是知识库内容缺口、暂时修不了的题（xfail）。
        # 它们**不计入门禁**，但必须留在案例集里持续显示——把红的案例删掉换来的绿色
        # 是假的，而这套评测存在的意义就是让缺口一直看得见。
        known_gap = bool(case.get("known_gap"))
        results.append({
            "id": case["id"],
            "domain": case["domain"],
            "ok": ok,
            "known_gap": known_gap,
            "query": case["query"],
            "ids": ids,
            "matched_ranks": matched_ranks,
            "risk": evidence.get("risk"),
            "missing_points": missing_points,
            "hit_forbidden": hit_forbidden,
            "checks": {
                "recall": recall_ok,
                "risk": risk_ok,
                "citation": citation_ok,
                "authority": authority_ok,
                "evidence_class": evidence_class_ok,
                "golden_points": golden_ok,
                "forbidden": forbidden_ok,
                "guard": guard_ok,
                "gap": gap_ok,
            },
        })
    gated = [row for row in results if not row["known_gap"]]
    gaps = [row for row in results if row["known_gap"]]
    passed = sum(1 for row in gated if row["ok"])
    # 标了 known_gap 却过了：缺口补上了，该把标记摘掉，让它进门禁
    closed_gaps = [row["id"] for row in gaps if row["ok"]]
    domains = sorted({str(row["domain"]) for row in results})
    domain_summary = {
        domain: {
            "cases": sum(1 for row in results if row["domain"] == domain),
            "passed": sum(1 for row in results if row["domain"] == domain and row["ok"]),
        }
        for domain in domains
    }
    return {
        "ok": passed == len(gated),
        "summary": {
            "cases": len(gated),
            "passed": passed,
            "failed": len(gated) - passed,
            "pass_rate": passed / len(gated) if gated else 0.0,
            "known_gaps": len(gaps),
            "closed_gaps": closed_gaps,
            "total_cases": len(results),
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
        f"passed={summary['passed']} failed={summary['failed']} pass_rate={summary['pass_rate']:.1%}"
        + (f" known_gaps={summary['known_gaps']}" if summary.get("known_gaps") else ""),
    ]
    if summary.get("closed_gaps"):
        lines.append(f"- 缺口已补上，可以摘掉 known_gap 标记：{','.join(summary['closed_gaps'])}")
    for row in result["results"]:
        ranks = ",".join(f"{key}:{value or '-'}" for key, value in row["matched_ranks"].items())
        status = "GAP " if row.get("known_gap") else ("PASS" if row["ok"] else "FAIL")
        line = f"- {status} {row['id']} | risk={row['risk']} | ranks={ranks}"
        if not row["ok"]:
            failed = [name for name, passed in row.get("checks", {}).items() if not passed]
            if failed:
                line += f" | failed={','.join(failed)}"
            if row.get("missing_points"):
                missing = ["/".join(str(o) for o in (g if isinstance(g, list) else [g])) for g in row["missing_points"]]
                line += f" | missing={';'.join(missing)}"
            if row.get("hit_forbidden"):
                line += f" | forbidden={','.join(row['hit_forbidden'])}"
        lines.append(line)
    return "\n".join(lines)
