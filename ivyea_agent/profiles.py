"""Account and ASIN operating profiles.

Profiles are deterministic local context for the agent: target ACoS, protected
terms, core terms, lifecycle stage, and operating notes. They complement free
form AGENTS.md with structured fields that tools can consume.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import config

PROFILE_FILE = config.IVYEA_DIR / "profiles.json"

DEFAULT_PROFILE: dict[str, Any] = {
    "site": "US",
    "target_acos": None,
    "stage": "",
    "protected_terms": [],
    "core_terms": [],
    "competitor_terms": [],
    "margin_rate": None,
    "breakeven_acos": None,
    "price": None,
    "currency": "USD",
    "listing_risks": [],
    "last_reviewed_at": "",
    "notes": "",
}


def _empty_store() -> dict[str, Any]:
    return {"default": dict(DEFAULT_PROFILE), "asins": {}, "stores": {}}


def _clean_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = values.split(",")
    out = []
    seen = set()
    for value in values:
        item = str(value or "").strip()
        key = item.lower()
        if item and key not in seen:
            out.append(item)
            seen.add(key)
    return out


def _clean_profile(raw: dict[str, Any] | None) -> dict[str, Any]:
    p = dict(DEFAULT_PROFILE)
    if raw:
        p.update(raw)
    for key in ("protected_terms", "core_terms", "competitor_terms", "listing_risks"):
        p[key] = _clean_list(p.get(key))
    for num_key in ("target_acos", "margin_rate", "breakeven_acos", "price"):
        if p.get(num_key) in ("", None):
            p[num_key] = None
            continue
        try:
            p[num_key] = float(p[num_key])
        except (TypeError, ValueError):
            p[num_key] = None
    p["site"] = str(p.get("site") or "").strip().upper() or "US"
    p["stage"] = str(p.get("stage") or "").strip()
    p["currency"] = str(p.get("currency") or "USD").strip().upper()
    p["last_reviewed_at"] = str(p.get("last_reviewed_at") or "").strip()
    p["notes"] = str(p.get("notes") or "").strip()
    return p


def load() -> dict[str, Any]:
    if not PROFILE_FILE.exists():
        return _empty_store()
    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty_store()
    out = _empty_store()
    out["default"] = _clean_profile(data.get("default") or {})
    for bucket in ("asins", "stores"):
        for key, value in (data.get(bucket) or {}).items():
            out[bucket][str(key)] = _clean_profile(value or {})
    return out


def save(data: dict[str, Any]) -> None:
    config.ensure_dirs()
    PROFILE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _bucket_for(key: str) -> tuple[str, str]:
    key = (key or "default").strip()
    if not key or key.lower() == "default":
        return "default", "default"
    if key.lower().startswith("sid:") or key.isdigit():
        sid = key.split(":", 1)[1] if key.lower().startswith("sid:") else key
        return "stores", f"sid:{sid}"
    return "asins", key.upper()


def get(key: str = "default") -> dict[str, Any]:
    data = load()
    bucket, norm = _bucket_for(key)
    if bucket == "default":
        return dict(data["default"])
    return dict(data.get(bucket, {}).get(norm, DEFAULT_PROFILE))


def update(key: str = "default", **fields: Any) -> dict[str, Any]:
    data = load()
    bucket, norm = _bucket_for(key)
    current = data["default"] if bucket == "default" else data[bucket].get(norm, {})
    merged = dict(current or {})
    for name, value in fields.items():
        if value is not None:
            merged[name] = value
    merged["last_reviewed_at"] = time.strftime("%Y-%m-%d")
    cleaned = _clean_profile(merged)
    if bucket == "default":
        data["default"] = cleaned
    else:
        data[bucket][norm] = cleaned
    save(data)
    return cleaned


def list_profiles() -> list[tuple[str, dict[str, Any]]]:
    data = load()
    rows = [("default", data["default"])]
    rows.extend((k, v) for k, v in sorted(data["asins"].items()))
    rows.extend((k, v) for k, v in sorted(data["stores"].items()))
    return rows


def resolve(*, asin: str = "", store: str = "") -> dict[str, Any]:
    """Merge default profile with a store or ASIN profile."""
    data = load()
    merged = dict(data["default"])
    key = store or asin
    if key:
        bucket, norm = _bucket_for(key)
        if bucket != "default":
            specific = data.get(bucket, {}).get(norm, {})
            for field, value in specific.items():
                if field in ("protected_terms", "core_terms", "competitor_terms", "listing_risks") and not value:
                    continue
                if field in ("target_acos", "margin_rate", "breakeven_acos", "price", "stage", "notes",
                             "last_reviewed_at") and value in ("", None):
                    continue
                merged[field] = value
    return _clean_profile(merged)


def context_text(profile: dict[str, Any], label: str = "default") -> str:
    p = _clean_profile(profile)
    parts = [f"[运营画像:{label}]", f"- 站点: {p['site']}"]
    if p.get("target_acos") is not None:
        parts.append(f"- 目标 ACOS: {p['target_acos']:.0%}")
    if p.get("margin_rate") is not None:
        parts.append(f"- 毛利率: {p['margin_rate']:.0%}")
    if p.get("breakeven_acos") is not None:
        parts.append(f"- 盈亏平衡 ACOS: {p['breakeven_acos']:.0%}")
    if p.get("price") is not None:
        parts.append(f"- 价格: {p['currency']} {p['price']:.2f}")
    if p.get("stage"):
        parts.append(f"- 生命周期阶段: {p['stage']}")
    if p.get("protected_terms"):
        parts.append("- 保护词: " + ", ".join(p["protected_terms"]))
    if p.get("core_terms"):
        parts.append("- 核心词: " + ", ".join(p["core_terms"]))
    if p.get("competitor_terms"):
        parts.append("- 竞品/对标词: " + ", ".join(p["competitor_terms"]))
    if p.get("listing_risks"):
        parts.append("- Listing 风险: " + ", ".join(p["listing_risks"]))
    if p.get("last_reviewed_at"):
        parts.append("- 画像复核日期: " + p["last_reviewed_at"])
    if p.get("notes"):
        parts.append("- 打法备注: " + p["notes"])
    return "\n".join(parts)


def render(profile: dict[str, Any], label: str = "default") -> str:
    return context_text(profile, label=label)
