"""P2 可执行动作模型 + 从规则引擎明细提取。

把巡检明细(decision_tag 等)转成结构化、可审核、可执行的动作：
- negative：否词（最干净、最安全，P2 主力）
- reduce_bid / scale_up：调 bid（需当前 bid 才能算绝对值，否则只作建议）
其余(hold_test/observe/listing_feedback/manual_review)只作信息，不执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# 默认调价幅度（受护栏 ≤20% 约束）
DEFAULT_REDUCE_PCT = -0.15
DEFAULT_SCALE_PCT = 0.15

EXECUTABLE_TAGS = {"negative_candidate", "reduce_bid", "scale_up"}


@dataclass
class Action:
    kind: str                       # negative | reduce_bid | scale_up
    search_term: str
    term_category: str = ""
    match_type: str = ""
    confidence: str = ""
    reason: str = ""
    clicks30: float = 0.0
    orders30: float = 0.0
    spend30: float = 0.0
    acos30: str = ""
    # bid 类
    change_pct: Optional[float] = None   # 相对调整幅度
    current_bid: Optional[float] = None
    new_bid: Optional[float] = None
    # 否词类
    negate_match: str = "negativeExact"
    # 审核/护栏
    blocked: bool = False
    block_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        if self.blocked:
            return False
        if self.kind == "negative":
            return bool(self.search_term)
        # bid 类需要当前 bid 才能算绝对值
        return self.new_bid is not None

    def summary(self) -> str:
        if self.kind == "negative":
            return f"否词[{self.negate_match}] “{self.search_term}”"
        arrow = "↓" if (self.change_pct or 0) < 0 else "↑"
        bid = ""
        if self.current_bid is not None and self.new_bid is not None:
            bid = f"（{self.current_bid:.2f}→{self.new_bid:.2f}）"
        elif self.change_pct is not None:
            bid = f"（{self.change_pct:+.0%}，缺当前bid暂作建议）"
        return f"调价{arrow} “{self.search_term}” {bid}"


_TAG_TO_KIND = {
    "negative_candidate": "negative",
    "reduce_bid": "reduce_bid",
    "scale_up": "scale_up",
}


def _f(v: Any) -> float:
    try:
        return float(str(v).replace("%", "").strip() or 0)
    except Exception:
        return 0.0


def extract_actions(detail_csv: str, current_bids: Optional[dict[str, float]] = None) -> list[Action]:
    """从明细 CSV 抽出可执行动作。current_bids: {search_term: 当前bid}（可选，用于算 bid 绝对值）。"""
    import pandas as pd
    df = pd.read_csv(detail_csv)
    df.columns = [str(c).lstrip("﻿") for c in df.columns]
    current_bids = current_bids or {}
    actions: list[Action] = []
    for _, r in df.iterrows():
        tag = str(r.get("decision_tag", "")).strip()
        if tag not in EXECUTABLE_TAGS:
            continue
        kind = _TAG_TO_KIND[tag]
        term = str(r.get("search_term", "")).strip()
        a = Action(
            kind=kind, search_term=term,
            term_category=str(r.get("term_category", "")).strip(),
            match_type=str(r.get("match_type", "")).strip(),
            confidence=str(r.get("confidence_level", "")).strip(),
            reason=str(r.get("decision_reason", "")).strip(),
            clicks30=_f(r.get("30d_clicks")), orders30=_f(r.get("30d_orders")),
            spend30=_f(r.get("30d_spend")), acos30=str(r.get("30d_acos", "")).strip(),
        )
        if kind in ("reduce_bid", "scale_up"):
            a.change_pct = DEFAULT_REDUCE_PCT if kind == "reduce_bid" else DEFAULT_SCALE_PCT
            cb = current_bids.get(term)
            if cb is not None:
                a.current_bid = cb
                a.new_bid = round(cb * (1 + a.change_pct), 2)
        actions.append(a)
    return actions


def load_detail_from_dir(patrol_dir: str) -> Optional[str]:
    """在巡检输出目录里找明细 CSV。"""
    for p in Path(patrol_dir).glob("*明细*.csv"):
        return str(p)
    return None
