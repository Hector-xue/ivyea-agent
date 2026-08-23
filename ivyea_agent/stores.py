"""店铺清单 —— 多店铺巡检的目标解析。

为什么独立成模块而不是在 schedule 里现拉：

1. **清单要缓存**。11 个店 × 每小时一次 L1，如果每次都拉一遍店铺列表，
   一天就是 792 次纯浪费的调用（清单几乎不变）。
2. **清单拉不到时不能让巡检停摆**。店铺列表是"巡检谁"的元数据，不是被巡检的
   数据本身。它挂了应当退回上次的结果继续巡检，而不是 11 个店一起哑掉——
   那等于把一个元数据故障放大成全店失明。
3. **能力标志位在这里**。领星的店铺列表自带 ``has_ads_setting``，实测
   TR/PL 两店为 0，且对它们调广告接口稳定返回 ``code=102 参数不合法``。
   有权威标志位就别去猜错误码语义（见 ``supports_ads``）。
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from . import config

STORES_FILE = config.IVYEA_DIR / "stores.json"

#: 店铺清单缓存时长。店铺增删是"人手动做的事"，6 小时足够新。
DEFAULT_TTL_SECONDS = 6 * 3600

#: 领星店铺状态：2 = 正常。其余值（停用/授权失效）不参与巡检。
STATUS_ACTIVE = 2


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """领星原始行 → 本模块的 canonical 形状。

    与 ``metrics`` 的数据源同理：上层只认这里的字段名，换供应商时只改这里。
    """
    return {
        "sid": row.get("sid"),
        "name": str(row.get("name") or "").strip() or f"sid {row.get('sid')}",
        "region": str(row.get("region") or "").strip(),
        "country": str(row.get("country") or "").strip(),
        "marketplace_id": str(row.get("marketplace_id") or "").strip(),
        "seller_id": str(row.get("seller_id") or "").strip(),
        "status": int(row.get("status") or 0),
        # 领星侧「该店是否已配置广告」。0 的店调广告接口必然失败。
        "has_ads": bool(int(row.get("has_ads_setting") or 0)),
    }


def _read_cache() -> dict[str, Any]:
    if not STORES_FILE.exists():
        return {}
    try:
        data = json.loads(STORES_FILE.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001 —— 缓存坏了当没有
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(stores: list[dict[str, Any]]) -> None:
    config.ensure_dirs()
    STORES_FILE.write_text(
        json.dumps({"fetched_at": time.time(), "stores": stores},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")


def list_stores(*, force: bool = False, ttl: float = DEFAULT_TTL_SECONDS,
                include_inactive: bool = False) -> list[dict[str, Any]]:
    """店铺清单。命中缓存则不联网。

    拉取失败时**回退到陈旧缓存**并照常返回——理由见模块文档第 2 条。
    连缓存都没有才抛错：那种情况下确实无从知道要巡检谁。
    """
    cache = _read_cache()
    cached = cache.get("stores") if isinstance(cache.get("stores"), list) else None
    fresh = cached is not None and (time.time() - float(cache.get("fetched_at") or 0)) < ttl

    if cached is not None and fresh and not force:
        stores = cached
    else:
        from . import lingxing_datasets
        try:
            stores = [_normalize(r) for r in lingxing_datasets.list_sellers()
                      if isinstance(r, dict) and r.get("sid") is not None]
            _write_cache(stores)
        except Exception:                               # noqa: BLE001
            if cached is not None:
                stores = cached                         # 陈旧缓存好过全店失明
            else:
                # 领星没配（或挂了且无缓存）时退到亚马逊那边登记的站点。
                # 不做这一步的话，只用亚马逊官方 API 的人**一个店都巡检不了**：
                # 目标解析拿不到清单，每条任务都报"缺少 sid"。
                stores = _amazon_stores()
                if not stores:
                    raise

    if include_inactive:
        return list(stores)
    return [s for s in stores if int(s.get("status") or 0) == STATUS_ACTIVE]


def _amazon_stores() -> list[dict[str, Any]]:
    """把亚马逊侧登记的站点当作店铺清单。

    形状与领星那份**逐字段对齐**（sid/name/region/country/marketplace_id/
    seller_id/status/has_ads），调用方无从分辨来源 —— 这正是目的：
    店铺清单是元数据，不该让上层为"你用的是哪家 ERP"分叉。
    """
    try:
        from . import amazon_auth
        rows = amazon_auth.marketplaces()
    except Exception:                                   # noqa: BLE001
        return []
    return [{
        "sid": m["sid"],
        "name": m["name"],
        "region": m["region"],
        "country": m["country"],
        "marketplace_id": m["marketplace_id"],
        "seller_id": m["seller_id"],
        "status": STATUS_ACTIVE,
        # 有广告档案 ID 才算开通广告 —— 与领星的 has_ads_setting 同一语义，
        # 广告类规则据此跳过而不是当成故障（ADR-0018）
        "has_ads": bool(m["ads_profile_id"]),
    } for m in rows]


def get(sid: Any, *, ttl: float = DEFAULT_TTL_SECONDS) -> Optional[dict[str, Any]]:
    """按 sid 取单店信息。取不到返回 None（调用方自行决定降级）。"""
    key = str(sid)
    try:
        stores = list_stores(ttl=ttl, include_inactive=True)
    except Exception:                                   # noqa: BLE001
        return None
    for s in stores:
        if str(s.get("sid")) == key:
            return s
    return None


def name_of(sid: Any) -> str:
    """店铺名，取不到就退回 ``sid N``——卡片标题不能因为清单挂了就空着。"""
    store = get(sid)
    return str(store["name"]) if store else f"sid {sid}"


def supports_ads(sid: Any) -> bool:
    """该店是否开通了广告。

    **清单取不到时返回 True**（按"支持"处理）：宁可发一次会失败的请求并如实
    报出数据缺口，也不要因为元数据缺失就静默跳过广告规则——后者会让人以为
    "没告警＝没问题"，正是 ADR-0017 明确反对的失败模式。
    """
    store = get(sid)
    return True if store is None else bool(store.get("has_ads"))


def resolve_targets(args: dict[str, Any]) -> list[dict[str, Any]]:
    """把任务参数解析成要巡检的店铺列表。

    支持三种写法，按优先级：
    - ``sids: "all"``            全部在营店铺
    - ``sids: [1863, 1872]``     指定多店
    - ``sid: 1863``              单店（旧写法，保持兼容）

    另有 ``exclude_sids`` 从结果里剔除。返回的每一项都带 ``name``/``has_ads``，
    调用方不必再自己查清单。
    """
    exclude = {str(v) for v in (args.get("exclude_sids") or [])}
    sids = args.get("sids")

    if isinstance(sids, str) and sids.strip().lower() == "all":
        targets = list_stores()
    elif sids:
        wanted = [str(v) for v in (sids if isinstance(sids, (list, tuple)) else [sids])]
        try:
            known = {str(s["sid"]): s for s in list_stores(include_inactive=True)}
        except Exception:                               # noqa: BLE001 —— 清单挂了也要能按 sid 跑
            known = {}
        targets = [known.get(w) or {"sid": w, "name": f"sid {w}", "has_ads": True}
                   for w in wanted]
    else:
        sid = args.get("sid")
        if sid is None or sid == "":
            return []
        store = get(sid)
        targets = [store or {"sid": sid, "name": f"sid {sid}", "has_ads": True}]

    return [t for t in targets if str(t.get("sid")) not in exclude]


# ── 站点时区 ────────────────────────────────────────────────────────────────
# 领星把促销活动时间给成**站点当地时间的裸字符串**（"2026-08-24 23:59:00"，
# 不带时区）。要算"还剩几小时结束"，必须先按店铺所在站点把它变成绝对时刻。
# 拿服务器时区去算，UK 的活动会差 7~8 小时 —— 正好是"以为还有一天、其实已经
# 结束了"这种最坏的错法。
#
# 键用 marketplace_id：它是亚马逊自己的常量，一个站点一个，永不变。店铺名是
# 用户起的，领星给的 country 是中文，两个都不能当键。
MARKETPLACE_TZ: dict[str, str] = {
    "ATVPDKIKX0DER": "America/Los_Angeles", "A2EUQ1WTGCTBG2": "America/Toronto",
    "A1AM78C64UM0Y8": "America/Mexico_City", "A2Q3Y263D00KWC": "America/Sao_Paulo",
    "A1F83G8C2ARO7P": "Europe/London", "A1PA6795UKMFR9": "Europe/Berlin",
    "A13V1IB3VIYZZH": "Europe/Paris", "APJ6JRA9NG5V4": "Europe/Rome",
    "A1RKKUPIHCS9HS": "Europe/Madrid", "A1805IZSGTT6HS": "Europe/Amsterdam",
    "A2NODRKZP88ZB9": "Europe/Stockholm", "A1C3SOZRARQ6R3": "Europe/Warsaw",
    "AMEN7PMS3EDWL": "Europe/Brussels", "A33AVAJ2PDY3EV": "Europe/Istanbul",
    "A17E79C6D8DWNP": "Asia/Riyadh", "A2VIGQ35RCS4UG": "Asia/Dubai",
    "A21TJRUUN4KGV": "Asia/Kolkata", "ARBP9OOSHTCHU": "Africa/Cairo",
    "A1VC38T7YXB528": "Asia/Tokyo", "A39IBJ37TRP1C6": "Australia/Sydney",
    "A19VAU5U5O7RUS": "Asia/Singapore",
}


def timezone_name(sid: Any) -> str:
    """该店铺所在站点的 IANA 时区名；认不出来退回 UTC。"""
    store = get(sid) or {}
    return MARKETPLACE_TZ.get(str(store.get("marketplace_id") or ""), "UTC")


def tzinfo(sid: Any):
    """时区对象。**绝不抛异常** —— 缺 tzdata 的精简环境退回 UTC，
    倒计时会不准，但整轮巡检不该因此挂掉（那才是更大的故障）。"""
    from datetime import timezone as _tz
    name = timezone_name(sid)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                                   # noqa: BLE001
        return _tz.utc
