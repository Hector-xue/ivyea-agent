"""Offer, margin, inventory, and ad scaling audit."""
from __future__ import annotations

from typing import Any


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def audit(*, price: float | None = None, competitor_price: float | None = None,
          margin_rate: float | None = None, target_acos: float | None = None,
          inventory_days: float | None = None, coupon: str = "",
          spend: float | None = None, sales: float | None = None) -> dict[str, Any]:
    risks: list[dict[str, str]] = []
    tasks: list[dict[str, str]] = []

    acos = (spend / sales) if spend is not None and sales and sales > 0 else None
    breakeven = margin_rate
    effective_target = target_acos if target_acos is not None else (margin_rate * 0.8 if margin_rate is not None else None)

    if margin_rate is not None and target_acos is not None and target_acos > margin_rate:
        risks.append({"area": "margin", "level": "high", "reason": f"目标 ACOS {_pct(target_acos)} 高于毛利率 {_pct(margin_rate)}，放量可能亏损。"})
    if acos is not None and breakeven is not None and acos > breakeven:
        risks.append({"area": "profit", "level": "high", "reason": f"当前广告 ACOS {_pct(acos)} 高于盈亏线 {_pct(breakeven)}。"})
    elif acos is not None and effective_target is not None and acos > effective_target:
        risks.append({"area": "efficiency", "level": "medium", "reason": f"当前广告 ACOS {_pct(acos)} 高于目标 {_pct(effective_target)}。"})

    if inventory_days is not None:
        if inventory_days < 14:
            risks.append({"area": "inventory", "level": "high", "reason": f"库存仅约 {inventory_days:.0f} 天，不适合激进放量。"})
        elif inventory_days < 30:
            risks.append({"area": "inventory", "level": "medium", "reason": f"库存约 {inventory_days:.0f} 天，放量需控制节奏。"})

    if price is not None and competitor_price is not None and competitor_price > 0:
        gap = (price - competitor_price) / competitor_price
        if gap > 0.15:
            risks.append({"area": "price", "level": "high", "reason": f"售价高于竞品约 {gap:.0%}。"})
        elif gap > 0.05:
            risks.append({"area": "price", "level": "medium", "reason": f"售价高于竞品约 {gap:.0%}。"})
        elif gap < -0.15 and margin_rate is not None and margin_rate < 0.25:
            risks.append({"area": "price", "level": "medium", "reason": "售价显著低于竞品且毛利偏低，需确认促销是否侵蚀利润。"})

    if coupon and coupon.strip():
        tasks.append({"type": "coupon", "action": f"复核 coupon：{coupon}，确认折后毛利仍覆盖目标 ACOS。"})
    elif any(r["area"] == "price" for r in risks):
        tasks.append({"type": "coupon", "action": "价格偏高但无 coupon 信息，评估是否用 coupon 改善点击后转化。"})

    if any(r["area"] == "inventory" and r["level"] == "high" for r in risks):
        ad_guidance = "先控预算/控 bid，避免库存断货；补货或确认库存后再放量。"
    elif any(r["area"] in ("margin", "profit") and r["level"] == "high" for r in risks):
        ad_guidance = "不要加预算；优先降本、收紧低效流量或调整目标 ACOS/价格。"
    elif any(r["area"] == "price" and r["level"] == "high" for r in risks):
        ad_guidance = "放量前先处理价格/coupon 承接；相关词低转化不一定该否。"
    elif risks:
        ad_guidance = "允许小步测试，预算和 bid 调整需保守并设复盘窗口。"
    else:
        ad_guidance = "Offer/库存/利润未发现明显阻力，可结合搜索词质量继续评估放量。"

    return {
        "acos": acos,
        "breakeven_acos": breakeven,
        "effective_target_acos": effective_target,
        "risks": risks,
        "tasks": tasks,
        "ad_guidance": ad_guidance,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        "# Offer / 库存 / 利润诊断",
        "",
        f"- 广告动作建议：{result['ad_guidance']}",
        f"- 当前广告 ACOS：{_pct(result.get('acos'))}",
        f"- 盈亏 ACOS：{_pct(result.get('breakeven_acos'))}",
        f"- 有效目标 ACOS：{_pct(result.get('effective_target_acos'))}",
        "",
        "## 风险",
    ]
    if not result["risks"]:
        lines.append("（未发现明显 Offer/库存/利润风险）")
    else:
        for r in result["risks"]:
            lines.append(f"- [{r['level']}] {r['area']}: {r['reason']}")
    lines.append("")
    lines.append("## 任务")
    if not result["tasks"]:
        lines.append("（暂无明确任务）")
    else:
        for t in result["tasks"]:
            lines.append(f"- {t['type']}: {t['action']}")
    return "\n".join(lines) + "\n"
