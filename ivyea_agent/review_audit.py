"""Review, Q&A, and offer trust audit for Amazon operations."""
from __future__ import annotations

import re
from typing import Any


_PATTERNS = {
    "quality": ("broken", "defect", "poor quality", "质量差", "坏了", "不耐用", "掉漆", "破损"),
    "not_working": ("not work", "doesn't work", "stopped working", "无法使用", "不能用", "失灵", "没反应"),
    "missing_parts": ("missing", "no cable", "no manual", "缺少", "少件", "没有说明书", "漏发"),
    "size_fit": ("too small", "too large", "fit", "尺寸", "太小", "太大", "不合适"),
    "expectation": ("not as described", "different from", "虚假", "不符", "和描述不一致", "误导"),
    "shipping": ("late", "damaged package", "shipping", "物流", "包装破损", "到货慢"),
    "support": ("support", "warranty", "refund", "客服", "售后", "保修", "退款"),
}


def _count(text: str, patterns: tuple[str, ...]) -> int:
    low = text.lower()
    return sum(len(re.findall(re.escape(p.lower()), low)) for p in patterns)


def audit(*, reviews: str = "", qa: str = "", rating: float | None = None,
          review_count: int | None = None, price: float | None = None,
          coupon: str = "", competitor_price: float | None = None) -> dict[str, Any]:
    text = "\n".join([reviews or "", qa or ""]).strip()
    issues = []
    for key, patterns in _PATTERNS.items():
        c = _count(text, patterns)
        if c:
            issues.append({"type": key, "count": c, "level": "high" if c >= 2 else "medium"})

    risks: list[dict[str, str]] = []
    if rating is not None:
        if rating < 4.0:
            risks.append({"area": "rating", "level": "high", "reason": f"评分 {rating:.1f} 低于 4.0，放量会放大转化损失。"})
        elif rating < 4.3:
            risks.append({"area": "rating", "level": "medium", "reason": f"评分 {rating:.1f} 偏弱，需谨慎放量。"})
    if review_count is not None:
        if review_count < 10:
            risks.append({"area": "review_count", "level": "high", "reason": f"评论数 {review_count} 很少，信任资产不足。"})
        elif review_count < 30:
            risks.append({"area": "review_count", "level": "medium", "reason": f"评论数 {review_count} 偏少，CVR 判断需保守。"})
    if price is not None and competitor_price is not None and competitor_price > 0:
        gap = (price - competitor_price) / competitor_price
        if gap > 0.15:
            risks.append({"area": "price", "level": "high", "reason": f"价格高于竞品约 {gap:.0%}，广告低转化可能是 offer 问题。"})
        elif gap > 0.05:
            risks.append({"area": "price", "level": "medium", "reason": f"价格高于竞品约 {gap:.0%}，需关注转化阻力。"})
    if coupon and any(k in coupon.lower() for k in ("none", "无", "no")) and risks:
        risks.append({"area": "coupon", "level": "medium", "reason": "存在信任/价格风险但无 coupon 支撑，建议评估促销承接。"})

    tasks = []
    issue_actions = {
        "quality": "汇总质量差评，确认是否需要产品页说明、质检或批次问题处理。",
        "not_working": "优先排查不可用/失灵问题，广告放量前先降低售后风险。",
        "missing_parts": "检查包装清单、说明书、配件图和发货 SOP。",
        "size_fit": "补充尺寸/适配范围图片和五点说明，减少误购。",
        "expectation": "修正文案或图片中可能造成误解的卖点表达。",
        "shipping": "区分物流问题和产品问题，必要时优化包装说明。",
        "support": "补充保修/售后承诺和 Q&A 回答。",
    }
    for issue in issues:
        tasks.append({"type": issue["type"], "action": issue_actions[issue["type"]]})

    high = any(r["level"] == "high" for r in risks) or any(i["level"] == "high" for i in issues)
    if high:
        ad_guidance = "不要激进放量；高花费低转化词先控 bid/限预算，优先修复 Review/Offer 信任问题。"
    elif risks or issues:
        ad_guidance = "保持小步测试；相关词不要急着否定，先处理信任/描述/促销承接。"
    else:
        ad_guidance = "未发现明显 Review/Offer 阻力，可继续结合广告数据判断收割、预算和出价。"

    return {
        "issues": issues,
        "risks": risks,
        "tasks": tasks,
        "ad_guidance": ad_guidance,
    }


def render(result: dict[str, Any]) -> str:
    lines = ["# Review / Q&A / Offer 归因", "", f"- 广告动作建议：{result['ad_guidance']}", ""]
    lines.append("## 归因标签")
    if not result["issues"]:
        lines.append("（未从文本中识别出明显差评/Q&A 问题）")
    else:
        for issue in result["issues"]:
            lines.append(f"- [{issue['level']}] {issue['type']} · 命中 {issue['count']} 次")
    lines.append("")
    lines.append("## 信任/报价风险")
    if not result["risks"]:
        lines.append("（未发现明显评分、评论数或价格风险）")
    else:
        for risk in result["risks"]:
            lines.append(f"- [{risk['level']}] {risk['area']}: {risk['reason']}")
    lines.append("")
    lines.append("## 修复任务")
    if not result["tasks"]:
        lines.append("（暂无明确修复任务）")
    else:
        for task in result["tasks"]:
            lines.append(f"- {task['type']}: {task['action']}")
    return "\n".join(lines) + "\n"
