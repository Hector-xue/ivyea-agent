"""巡检编排：规则引擎 → LLM 复核 → 合并报告。"""
from __future__ import annotations

from typing import Any, Optional

from . import config, report, review, rule_engine


def patrol(csv_path: str, asin: Optional[str] = None, site: Optional[str] = None,
           target_acos: Optional[float] = None, report_type: Optional[str] = None,
           output_dir: Optional[str] = None, use_llm: bool = True) -> dict[str, Any]:
    """跑一次只读巡检，返回 {text, md_path, rule_output, review}。"""
    settings = config.load_settings()
    site = site or settings.get("site", "US")
    target_acos = target_acos if target_acos is not None else settings.get("target_acos")

    rule_output = rule_engine.run(
        csv_path, asin=asin, site=site, report_type=report_type, output_dir=output_dir)

    out_dir = output_dir or str(__import__("pathlib").Path(csv_path).resolve().parent / "ivyea_patrol_out")

    if use_llm:
        provider = settings.get("provider", "deepseek")
        model = settings.get("model", "deepseek-chat")
        api_key = config.get_api_key(provider)
        rev = review.review(rule_output, provider, api_key, model, target_acos=target_acos)
    else:
        rev = {"ok": False, "markdown": "", "note": "已用 --no-llm 跳过 AI 复核。"}

    text = report.build(rule_output, rev)
    md_path = report.write_md(text, out_dir, asin=(rule_output.get("summary", {}).get("asin") or asin or ""))
    return {"text": text, "md_path": md_path, "rule_output": rule_output, "review": rev}
