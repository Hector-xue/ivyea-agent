"""Token 用量与成本核算（估算，单位人民币元/¥）。

价格随厂商调整，这里内置常见模型的近似单价（元 / 百万 token），可被 settings 的
`pricing_overrides` 覆盖。用于 `/cost` 与状态栏的成本提示——标注为**估算**。
"""
from __future__ import annotations

from typing import Any

from . import config

# 模型 id → {input, cached_input, output}（¥ / 1M tokens，近似值）
_PRICES: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 2.0, "cached_input": 0.5, "output": 8.0},
    "deepseek-reasoner": {"input": 4.0, "cached_input": 1.0, "output": 16.0},
    "default": {"input": 4.0, "cached_input": 1.0, "output": 12.0},
}


def _table() -> dict[str, dict[str, float]]:
    t = dict(_PRICES)
    ov = config.get_setting("pricing_overrides", {}) or {}
    if isinstance(ov, dict):
        t.update(ov)
    return t


def price_for(model: str) -> dict[str, float]:
    t = _table()
    return t.get(model) or t.get("default")


def estimate(model: str, usage: dict[str, Any]) -> float:
    """按 usage 估算本次成本（¥）。usage 用 OpenAI 字段：
    prompt_tokens / completion_tokens，及（DeepSeek）prompt_cache_hit_tokens。"""
    p = price_for(model)
    prompt = float(usage.get("prompt_tokens") or 0)
    completion = float(usage.get("completion_tokens") or 0)
    cached = float(usage.get("prompt_cache_hit_tokens")
                   or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    fresh = max(0.0, prompt - cached)
    return (fresh * p["input"] + cached * p["cached_input"] + completion * p["output"]) / 1_000_000.0


class UsageMeter:
    """累计一段会话的 token 与成本。"""

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0
        self.cached = 0
        self.cost = 0.0
        self.turns = 0

    def add(self, model: str, usage: dict[str, Any]) -> float:
        if not usage:
            return 0.0
        self.prompt += int(usage.get("prompt_tokens") or 0)
        self.completion += int(usage.get("completion_tokens") or 0)
        self.cached += int(usage.get("prompt_cache_hit_tokens")
                           or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        c = estimate(model, usage)
        self.cost += c
        self.turns += 1
        return c

    def summary(self) -> str:
        return (f"本会话 {self.turns} 轮 · 输入 {self.prompt}（缓存 {self.cached}）"
                f"/ 输出 {self.completion} token · 估算 ¥{self.cost:.4f}")
