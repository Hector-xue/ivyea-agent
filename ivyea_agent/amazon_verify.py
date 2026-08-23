"""亚马逊接入自检 —— 「填完凭据点一下，看到底通没通」。

分步报，不给一个笼统的红叉：LWA 换 token、SP-API 打一次真接口、
Ads API 列一次档案，是三件独立的事，任何一件失败的原因都不一样。
用户最常见的状态是"SP-API 批下来了、广告 API 还在排队"——
那种情况下前两步该是绿的，第三步是黄的，不是整体失败。
"""
from __future__ import annotations

from typing import Any

from . import amazon_auth


def _step(name: str, ok: bool, detail: str, hint: str = "") -> dict[str, Any]:
    return {"step": name, "ok": ok, "detail": detail, "hint": hint}


def verify() -> dict[str, Any]:
    """真的去打一次亚马逊。返回逐步结果 + 总体结论。"""
    steps: list[dict[str, Any]] = []
    st = amazon_auth.status()

    if not st["configured"]:
        steps.append(_step("凭据", False, "未填 client_id / client_secret / refresh_token",
                           "在 IvyeaOps 系统配置 → 亚马逊官方 API 里填；"
                           "三者来自开发者中心的 LWA 应用与授权回调"))
        return {"ok": False, "steps": steps, "status": st}
    steps.append(_step("凭据", True, f"已配置，区域 {st['region']}（{st['spapi_host']}）"))

    # 1) LWA
    try:
        amazon_auth.access_token(force=True)
        steps.append(_step("LWA 换 token", True, "成功"))
    except Exception as exc:                            # noqa: BLE001
        steps.append(_step("LWA 换 token", False, str(exc),
                           "invalid_client = client_id/secret 不对；"
                           "invalid_grant = refresh_token 失效或与该应用不匹配"))
        return {"ok": False, "steps": steps, "status": st}

    # 2) SP-API：拿第一个站点打一次真接口
    mkts = amazon_auth.marketplaces()
    if not mkts:
        steps.append(_step("SP-API", False, "还没登记任何站点",
                           "至少填一个 marketplace_id（例如美国 ATVPDKIKX0DER）"))
    else:
        mkt = mkts[0]
        try:
            from . import amazon_spapi
            rows = amazon_spapi.inventory_summaries(mkt["marketplace_id"])
            steps.append(_step("SP-API", True,
                               f"{mkt['name']} 库存接口通了，{len(rows)} 个 MSKU"))
        except Exception as exc:                        # noqa: BLE001
            steps.append(_step("SP-API", False, str(exc),
                               "403 多半是应用没被授权到这个站点，或 refresh_token "
                               "是在别的卖家账号下签的"))

    # 3) Ads API
    if not amazon_auth.is_configured(ads=True):
        steps.append(_step("Ads API", False, "未配置广告凭据",
                           "广告 API 通常单独审批。没批下来也不影响库存/订单那部分"))
    else:
        try:
            from . import amazon_ads
            profiles = amazon_ads.profiles()
            names = "、".join(
                f"{p.get('countryCode')}#{p.get('profileId')}" for p in profiles[:6])
            steps.append(_step("Ads API", True,
                               f"{len(profiles)} 个广告档案：{names or '（空）'}",
                               "把对应站点的 profileId 填进站点表，广告规则才知道去问谁"))
        except Exception as exc:                        # noqa: BLE001
            steps.append(_step("Ads API", False, str(exc),
                               "401 常见于漏了 Amazon-Advertising-API-Scope，"
                               "或这套 LWA 应用没开广告权限"))

    return {"ok": all(s["ok"] for s in steps), "steps": steps, "status": st}


def list_profiles() -> dict[str, Any]:
    """列广告档案，供界面上直接选 profileId —— 让人去后台抄一串数字是配置流程里
    最容易抄错的一步（抄错的表现是"保存成功但广告数据一直是空的"）。"""
    if not amazon_auth.is_configured(ads=True):
        return {"ok": False, "error": "未配置广告凭据", "profiles": []}
    try:
        from . import amazon_ads
        rows = amazon_ads.profiles()
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": str(exc), "profiles": []}
    return {"ok": True, "profiles": [{
        "profile_id": str(p.get("profileId") or ""),
        "country": str(p.get("countryCode") or ""),
        "currency": str(p.get("currencyCode") or ""),
        "type": str(((p.get("accountInfo") or {}).get("type")) or ""),
        "name": str(((p.get("accountInfo") or {}).get("name")) or ""),
        "marketplace_id": str(((p.get("accountInfo") or {}).get("marketplaceStringId")) or ""),
    } for p in rows]}


def render(result: dict[str, Any]) -> str:
    """CLI 输出。"""
    lines = ["== 亚马逊官方 API 自检 =="]
    for s in result.get("steps", []):
        mark = "✓" if s["ok"] else "✗"
        lines.append(f"  {mark} {s['step']}：{s['detail']}")
        if not s["ok"] and s.get("hint"):
            lines.append(f"      提示：{s['hint']}")
    st = result.get("status") or {}
    if st:
        lines.append(f"  站点 {st.get('marketplace_count', 0)} 个，"
                     f"其中 {st.get('with_ads_profile', 0)} 个填了广告档案")
    return "\n".join(lines) + "\n"
