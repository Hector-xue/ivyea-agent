from __future__ import annotations

import os
from typing import Callable, Optional

from .base import LLMError, LLMProvider, from_settings

__all__ = ["LLMError", "LLMProvider", "from_settings", "build_chain"]


def _normalize_fallbacks(val) -> list:
    """settings.fallback_models 可为 list 或逗号串。"""
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        return [x.strip() for x in val.split(",") if x.strip()]
    return []


def build_chain(model_cfg: dict, api_key: str,
                narrate: Optional[Callable[[str], None]] = None) -> LLMProvider:
    """主脑 + settings.fallback_models 里有 key 的备用，组成降级链。
    无可用备用时直接返回单一 provider（不套链，省一层）。"""
    from .. import config, models
    from .chain import ChainProvider

    primary = from_settings(model_cfg, api_key)
    members = [(primary, model_cfg.get("label") or model_cfg.get("model") or "主脑")]

    config.load_env()
    for mid in _normalize_fallbacks(config.get_setting("fallback_models", [])):
        m = models.by_id(mid)
        if not m or m.get("kind") not in ("openai", "anthropic"):
            continue
        key2 = os.environ.get(m.get("key_env", ""), "")
        if not key2:
            continue   # 没配 key 的备用跳过
        cfg2 = {"kind": m["kind"], "model": m.get("model"), "base": m.get("base"),
                "base_url": m.get("base"), "key_env": m.get("key_env"), "label": m.get("label")}
        try:
            members.append((from_settings(cfg2, key2), m.get("label") or mid))
        except LLMError:
            continue
    if len(members) == 1:
        return primary
    return ChainProvider(members, narrate)
