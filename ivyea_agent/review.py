"""LLM 复核层。

对规则引擎的确定性结论做复核：词分类、低效词归因、护栏检查、可执行说明。
规则引擎是骨架（确定性），LLM 只复核与措辞——不推翻数据，只补判断与解释。
无模型 key 时优雅降级（返回提示，不报错）。
"""
from __future__ import annotations

from typing import Any, Optional

from .knowledge import METHODOLOGY
from .providers import LLMError

REVIEW_INSTRUCTION = """\
下面是「规则引擎」对一份亚马逊搜索词报告产出的确定性结论（报告原文 + 基准数据）。
请基于上述方法论做 AI 复核，输出一段 Markdown（不要重复整篇报告），包含：

1. 词分类核对：对报告中点名的搜索词，标注主标签（品牌/竞品/属性/场景/核心品类/无关），并指出规则引擎可能误判的地方。
2. 低效词归因：对否词候选/控成本词，归因到（无关流量 / Listing承接不足 / 价格或转化问题 / 数据不足 / 需人工看Listing）之一。
3. 护栏检查：逐条检查建议是否触碰护栏（不否同义词/品牌词/竞品词/小类目核心词/保护词；单次降bid≤20%）。若有风险，明确指出"不建议直接执行，原因…"。
4. 可执行说明：对放量/否词/降bid 各给一句动作级说明。

要求：结论绑证据标签 [报告]（来自报表数据）/[推断]（基于现有信息判断）；不把猜测写成事实；简洁、专业、可执行。
"""


def review(rule_output: dict[str, Any], provider, target_acos: Optional[float] = None) -> dict[str, Any]:
    """返回 {ok, markdown, note}。provider 为 None（未配模型）时优雅降级，不抛异常。"""
    if provider is None:
        return {"ok": False, "markdown": "",
                "note": "未配置可用主脑模型，已跳过 AI 复核（仅输出规则引擎结论）。"
                        "用 `ivyea model` 或 `ivyea config` 配置。"}
    summary = rule_output.get("summary", {})
    report_md = rule_output.get("report_md", "")
    tgt = f"\n- 目标 ACoS：{target_acos:.0%}" if target_acos else ""
    user = (
        f"## 规则引擎报告原文\n{report_md}\n\n"
        f"## 基准/统计（summary.json 摘录）\n"
        f"- ASIN：{summary.get('asin')}　站点：{summary.get('site')}　报告类型：{summary.get('report_type')}\n"
        f"- ASIN 广告 CVR 基准：{summary.get('asin_baseline_cvr')}　ACOS 基准：{summary.get('asin_baseline_acos')}{tgt}\n"
        f"- 竞品词根：{summary.get('competitor_tokens')}\n\n"
        f"{REVIEW_INSTRUCTION}"
    )
    try:
        md = provider.complete(METHODOLOGY, user, json_mode=False, temperature=0.2)
        return {"ok": True, "markdown": md.strip(), "note": ""}
    except LLMError as e:
        return {"ok": False, "markdown": "", "note": f"AI 复核调用失败：{e}（已保留规则引擎结论）"}
