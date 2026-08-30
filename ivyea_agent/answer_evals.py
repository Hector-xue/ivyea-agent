"""答案级评测：真跑主脑生成回答，再按 rubric 判分。

和 `knowledge_quality` 的分工是刻意分开的：

- `knowledge_quality` 是**确定性层**，不调模型，秒级，进门禁。它只回答一件事：
  "回答这题所需要的证据，检索有没有给到？"
- 这一层是**判分层**，真跑主脑生成回答、再让 rubric 判分。它回答的是
  "拿到证据之后，答得对不对？"——那才是用户真正感受到的专业能力。

判分层慢、要花钱、分数还有波动，所以**不进默认测试套**，只做独立命令按需/定时跑，
结果落基线文件供前后对比。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import config, critique, knowledge, knowledge_quality


#: 判分只在这些域上做。剩下的题（注册材料、费率数字这类）用确定性层的召回断言
#: 就够了，让模型判它们既贵又不稳。
_JUDGE_DOMAINS = {
    "amazon_ads", "amazon_ads_products", "traffic_governance", "fba",
    "account_health", "brand_protection", "returns_claims",
}

ANSWER_SYSTEM = (
    "你是资深亚马逊运营专家。只依据给定证据作答；证据不足就直接说明知识缺口，"
    "不要把未经证据支持的说法写成亚马逊官方规则。"
    "严格区分「亚马逊官方事实」「账户数据推断」「运营经验/算法假设」。"
    "采用证据的句子末尾标注对应的 [K#] 引用键。"
)


def baseline_path() -> Path:
    return config.IVYEA_DIR / "evals" / "answer_baseline.json"


def cases(domains: set[str] | None = None) -> list[dict[str, Any]]:
    """挑出适合判分的案例。known_gap 的题也留着——那些正是最该看模型怎么处理缺口的。"""
    wanted = domains or _JUDGE_DOMAINS
    return [row for row in knowledge_quality.cases() if str(row.get("domain")) in wanted]


def _rubric_kind(domain: str) -> str:
    return "ads" if domain.startswith("amazon_ads") else "knowledge"


def run(limit: int = 0, domains: set[str] | None = None) -> dict[str, Any]:
    """跑一轮判分。provider 沿用主脑配置，不另设 judge 模型。"""
    from .providers import from_settings

    api_key = config.get_active_key()
    if not api_key:
        return {"ok": False, "note": "未配 key，无法跑答案级评测。", "results": []}
    provider = from_settings(config.get_model_config(), api_key)

    rows = cases(domains)
    if limit:
        rows = rows[:limit]

    results: list[dict[str, Any]] = []
    for case in rows:
        query = str(case["query"])
        evidence = knowledge.evidence_context(query, limit=int(case.get("limit") or 5))
        prompt = (
            f"【问题】\n{query}\n\n"
            f"【可用证据】\n{evidence.get('text') or '（内部知识库没有命中）'}"
        )
        row: dict[str, Any] = {
            "id": case["id"],
            "domain": case["domain"],
            "query": query,
            "known_gap": bool(case.get("known_gap")),
            "citations": len(evidence.get("citations") or []),
        }
        try:
            answer = provider.complete(ANSWER_SYSTEM, prompt, json_mode=False, temperature=0.2)
        except Exception as exc:      # noqa: BLE001
            row.update({"ok": False, "note": f"生成失败：{exc}"})
            results.append(row)
            continue

        answer = (answer or "").strip()
        # 引证键必须落在真发过去的那几个键上——判分之前先把编造引用挡掉，
        # 这一项是确定性的，不该交给 rubric 去凭感觉判。
        citation_check = knowledge.validate_citations(answer, evidence.get("citations") or [])
        verdict = critique.critique(
            task=query, answer=answer, provider=provider,
            kind=_rubric_kind(str(case["domain"])),
        )
        row.update({
            "ok": bool(citation_check.get("ok")) and not verdict.get("needs_fix"),
            "citation_ok": bool(citation_check.get("ok")),
            "needs_fix": bool(verdict.get("needs_fix")),
            "answer_chars": len(answer),
            "critique": str(verdict.get("markdown") or "")[:1200],
            "note": str(verdict.get("note") or ""),
        })
        results.append(row)

    passed = sum(1 for row in results if row.get("ok"))
    return {
        "ok": bool(results) and passed == len(results),
        "summary": {
            "cases": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": passed / len(results) if results else 0.0,
            "model": config.get_model_config().get("model", ""),
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "results": results,
    }


def save_baseline(result: dict[str, Any]) -> Path:
    path = baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_baseline() -> dict[str, Any] | None:
    path = baseline_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:      # noqa: BLE001
        return None


def render(result: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    if not result.get("results"):
        return f"答案级评测未运行：{result.get('note') or '没有可跑的案例'}"
    summary = result["summary"]
    lines = [
        "Ivyea 亚马逊答案级评测（真跑主脑 + rubric 判分）：",
        f"- result={'PASS' if result['ok'] else 'FAIL'} cases={summary['cases']} "
        f"passed={summary['passed']} failed={summary['failed']} "
        f"pass_rate={summary['pass_rate']:.1%} model={summary['model']}",
    ]
    if baseline and baseline.get("summary"):
        before = baseline["summary"].get("pass_rate", 0.0)
        delta = summary["pass_rate"] - before
        lines.append(f"- 对比基线：{before:.1%} → {summary['pass_rate']:.1%}（{delta:+.1%}）")
    for row in result["results"]:
        mark = "PASS" if row.get("ok") else "FAIL"
        tags = []
        if row.get("known_gap"):
            tags.append("known_gap")
        if not row.get("citation_ok", True):
            tags.append("引证不实")
        if row.get("needs_fix"):
            tags.append("rubric建议修正")
        suffix = f" | {','.join(tags)}" if tags else ""
        lines.append(f"- {mark} {row['id']} | cites={row.get('citations')}{suffix}")
        if row.get("note"):
            lines.append(f"    note: {row['note']}")
    return "\n".join(lines)
