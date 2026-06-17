"""领星 sid 维度广告规则引擎（确定性）—— 移植自 ivyea-ops lingxing_optimizer.py。

窗口逐日聚合（丢最近 N 天归因滞后）→ 毛利率推目标 ACOS（平衡=毛利，目标=factor×毛利）→
五杠杆候选：

  否词  — 搜索词 ≥N 点击且 0 单
  收割  — 搜索词 ≥N 单且 ACOS 健康 → 建议升精准（advisory）
  降bid — 关键词高 ACOS（≥min 点击）→ 新 bid = RPC×目标，单步封顶
  加bid — 赢家（≥min 单，ACOS ≤ 0.8×目标）→ +步长，≤ RPC×目标
  加预算 — 活动预算打满且盈利 → +步长

确定性、可审计；LLM 只复核不发明。冷却/历史否决走 agent 自有 memory（非 ivyea-ops 工单）。
M1 只读：产出候选清单 + 报告，不写入。每个杠杆独立 try/except，缺数据集不影响其它杠杆。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from . import config, memory
from .lingxing_cache import fetch_dataset   # 带缓存的取数（签名同 lingxing_datasets）
from .lingxing_openapi import LingXingError

# settings 键 → 默认值（移植自 ivyea-ops hub_settings）
_DEFAULTS = {
    "lingxing_neg_min_clicks": 15, "lingxing_bid_min_clicks": 15,
    "lingxing_scale_min_orders": 3, "lingxing_harvest_min_orders": 3,
    "lingxing_bid_step_pct": 15, "lingxing_cooldown_days": 7,
    "lingxing_opt_window_days": 30, "lingxing_opt_exclude_recent_days": 2,
    "lingxing_target_acos_factor": 0.7, "lingxing_bid_floor": 0.02,
    "lingxing_target_acos_override": 0, "lingxing_margin_override": 0,
}


def _cfg(key: str) -> Any:
    return config.get_setting(key, _DEFAULTS.get(key))


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _window_dates(win: int, excl: int) -> list[str]:
    return [(datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%d")
            for d in range(excl + 1, excl + 1 + win)]


def _bucket() -> dict[str, float]:
    return {"spend": 0.0, "sales": 0.0, "orders": 0.0, "clicks": 0.0, "impressions": 0.0}


def _add(b: dict[str, float], r: dict[str, Any]) -> None:
    b["spend"] += _f(r.get("cost")); b["sales"] += _f(r.get("sales"))
    b["orders"] += _f(r.get("orders")); b["clicks"] += _f(r.get("clicks"))
    b["impressions"] += _f(r.get("impressions"))


def _metrics(b: dict[str, float]) -> dict[str, Any]:
    s, sa, ck, im, od = b["spend"], b["sales"], b["clicks"], b["impressions"], b["orders"]
    return {"spend": round(s, 2), "sales": round(sa, 2), "orders": int(od), "clicks": int(ck),
            "impressions": int(im), "acos": (s / sa) if sa else None, "cpc": (s / ck) if ck else None,
            "rpc": (sa / ck) if ck else None, "cvr": (od / ck) if ck else None,
            "aov": (sa / od) if od else None}


def _agg(sid: int, dataset: str, dates: list[str], key_fn: Callable[[dict], Any],
         capture: tuple[str, ...], progress: Optional[Callable] = None, label: str = "") -> dict[Any, dict[str, Any]]:
    """窗口逐日聚合一个报表，按 key_fn 分桶，记录静态字段。缺某日静默跳过。"""
    out: dict[Any, dict[str, Any]] = {}
    total = len(dates)
    for i, day in enumerate(dates, 1):
        if progress:
            progress(label, i, total)
        try:
            rows = fetch_dataset(dataset, {"sid": sid, "report_date": day, "length": 300})
        except LingXingError:
            continue
        for r in rows:
            k = key_fn(r)
            if k is None:
                continue
            b = out.get(k)
            if b is None:
                b = {"_b": _bucket()}
                for c in capture:
                    b[c] = r.get(c)
                out[k] = b
            _add(b["_b"], r)
    return out


def _bid_map(sid: int) -> dict[str, dict[str, Any]]:
    m: dict[str, dict[str, Any]] = {}
    for offset in range(0, 3000, 300):
        try:
            rows = fetch_dataset("sp_keywords", {"sid": sid, "length": 300, "offset": offset})
        except LingXingError:
            break
        for k in rows:
            m[str(k.get("keyword_id"))] = {"bid": _f(k.get("bid")), "state": k.get("state")}
        if len(rows) < 300:
            break
    return m


def _campaign_budgets(sid: int) -> dict[str, dict[str, Any]]:
    m: dict[str, dict[str, Any]] = {}
    try:
        for c in fetch_dataset("sp_campaigns", {"sid": sid, "length": 300}):
            m[str(c.get("campaign_id"))] = {"daily_budget": _f(c.get("daily_budget")),
                                            "state": c.get("state"), "name": c.get("name")}
    except LingXingError:
        pass
    return m


def _store_margin(sid: int) -> Optional[float]:
    """店铺均值毛利率（fraction），取近 7 天利润报表。"""
    end = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        rows = fetch_dataset("asin_profit", {"sids": str(sid), "startDate": start, "endDate": end, "length": 500})
    except LingXingError:
        return None
    rates = []
    for r in rows:
        gr = r.get("grossRate")
        if gr in (None, ""):
            continue
        v = _f(gr)
        if v > 1:
            v /= 100.0
        if 0 < v < 1:
            rates.append(v)
    return (sum(rates) / len(rates)) if rates else None


def _store_key(sid: int) -> str:
    return f"sid:{sid}"


def _rejected(sid: int, name: str, kind: str) -> bool:
    try:
        return memory.was_rejected(_store_key(sid), name, kind)
    except Exception:
        return False


def _in_cooldown(sid: int, name: str, cooldown_days: int) -> bool:
    """该 store+目标 在冷却期内被批准过（任一调价/否词类）→ 冷却中。"""
    try:
        d = memory.days_since_last_approve(_store_key(sid), name,
                                           kinds=("reduce_bid", "scale_up", "negative", "budget"))
        return d is not None and d < cooldown_days
    except Exception:
        return False


def run_store(sid: int, days: Optional[int] = None, progress: Optional[Callable] = None) -> dict[str, Any]:
    """对一个店铺跑只读规则引擎，返回 {sid, window_days, margin, target_acos, candidates...}。"""
    factor = _f(_cfg("lingxing_target_acos_factor")) or 0.7
    neg_clicks = int(_cfg("lingxing_neg_min_clicks") or 15)
    bid_clicks = int(_cfg("lingxing_bid_min_clicks") or 15)
    scale_orders = int(_cfg("lingxing_scale_min_orders") or 3)
    harvest_orders = int(_cfg("lingxing_harvest_min_orders") or 3)
    step = (_f(_cfg("lingxing_bid_step_pct")) or 15) / 100.0
    floor = _f(_cfg("lingxing_bid_floor")) or 0.02
    cooldown = int(_cfg("lingxing_cooldown_days") or 7)
    excl = int(_cfg("lingxing_opt_exclude_recent_days") or 2)
    win = int(days or _cfg("lingxing_opt_window_days") or 30)
    t_over = _f(_cfg("lingxing_target_acos_override"))
    m_over = _f(_cfg("lingxing_margin_override"))
    dates = _window_dates(win, excl)

    margin = None if t_over > 0 else _store_margin(sid)
    if t_over > 0:
        breakeven, target = None, t_over
        note = f"目标ACOS=手动设定 {t_over:.0%}"
    elif m_over > 0:
        margin = m_over; breakeven = margin; target = factor * margin
        note = f"毛利率=手动 {margin:.0%}，目标ACOS={target:.0%}"
    elif margin:
        breakeven = margin; target = factor * margin
        note = f"毛利率≈{margin:.0%}(店铺均值)，目标ACOS={target:.0%}(={factor:g}×毛利)"
    else:
        breakeven = None; target = 0.30
        note = "未取到毛利数据，暂用默认目标ACOS 30%"

    def tgt() -> float:
        return target

    def brk() -> Optional[float]:
        return breakeven

    cands: list[dict[str, Any]] = []

    def guard(name: str, kind: str) -> tuple[bool, str]:
        """返回 (是否放行, 拦截原因)。历史否决/冷却拦截。"""
        if _rejected(sid, name, kind):
            return False, "记忆：上次人工已否决"
        if _in_cooldown(sid, name, cooldown):
            return False, f"冷却期内（{cooldown}天）刚动过"
        return True, ""

    # ---- 否词 + 收割：搜索词报表 ----
    try:
        st = _agg(sid, "sp_search_term_report", dates,
                  lambda r: (str(r.get("campaign_id")), str(r.get("query") or "")) if r.get("query") else None,
                  capture=("query", "campaign_id", "ad_group_id", "match_type"),
                  progress=progress, label="搜索词报表")
        for (cid, q), bk in st.items():
            m = _metrics(bk["_b"])
            if m["clicks"] >= neg_clicks and m["orders"] == 0:
                ok, why = guard(q, "negative")
                cands.append({
                    "lever": "否词", "op_type": "negate_keyword", "sid": sid, "campaign_id": cid,
                    "target_name": q, "metrics": m, "opt_target": tgt(), "opt_breakeven": brk(),
                    "blocked": not ok, "block_reason": why,
                    "rule": f"搜索词「{q}」{m['clicks']}点击/0单（≥{neg_clicks}点击）→ 否定(negativeExact)",
                    "significance": f"{m['clicks']}点击 0单 · 花费{m['spend']}",
                    "rationale": f"近{win}天该搜索词 {m['clicks']} 次点击 0 转化、花费 {m['spend']}，纯无效花费，建议否定。",
                })
            elif m["orders"] >= harvest_orders and m["acos"] is not None and (brk() is None or m["acos"] <= brk()):
                sug = round((m["rpc"] or 0) * tgt(), 2)
                cands.append({
                    "lever": "收割", "op_type": "add_keyword", "advisory": True, "sid": sid,
                    "campaign_id": cid, "target_name": q, "metrics": m,
                    "opt_target": tgt(), "opt_breakeven": brk(), "blocked": False, "block_reason": "",
                    "suggested_bid": sug,
                    "rule": f"搜索词「{q}」{m['orders']}单（≥{harvest_orders}）、ACOS {m['acos']:.0%} → 收割成精准词",
                    "significance": f"{m['orders']}单 ACOS {m['acos']:.0%}",
                    "rationale": f"该搜索词 {m['orders']} 单、ACOS {m['acos']:.0%} 健康，建议加入精准活动（建议bid≈{sug}）并在原活动否定它（毕业）。",
                })
    except LingXingError as e:
        cands.append({"lever": "错误", "target_name": "搜索词报表", "blocked": True,
                      "block_reason": str(e), "metrics": {}, "rule": "", "rationale": str(e)})

    # ---- 降bid / 加bid：关键词报表 + 实时 bid ----
    try:
        kr = _agg(sid, "sp_keyword_report", dates,
                  lambda r: str(r.get("keyword_id")) if r.get("keyword_id") else None,
                  capture=("keyword_id", "keyword_text", "match_type", "campaign_id"),
                  progress=progress, label="关键词报表")
        bids = _bid_map(sid) if kr else {}
        for kid, bk in kr.items():
            m = _metrics(bk["_b"])
            cur = bids.get(kid, {}).get("bid")
            name = bk.get("keyword_text") or kid
            if m["clicks"] < bid_clicks:
                continue
            T, B = tgt(), brk()
            if m["acos"] is not None and m["acos"] > T and cur:
                ideal = (m["rpc"] or 0) * T
                new_bid = max(floor, min(ideal, cur * (1 - step)))
                if new_bid < cur * 0.98:
                    ok, why = guard(name, "reduce_bid")
                    cands.append(_bid_cand("降bid", sid, kid, name, cur, round(new_bid, 2), m, T, B, ok, why,
                        f"ACOS {m['acos']:.0%} > 目标 {T:.0%}（{m['clicks']}点击≥{bid_clicks}）→ 降bid至 RPC×目标，单步≤{int(step*100)}%",
                        f"高ACOS控本：{cur}→{round(new_bid,2)}"))
            elif m["orders"] == 0 and cur:
                new_bid = max(floor, cur * (1 - step))
                if new_bid < cur * 0.98:
                    ok, why = guard(name, "reduce_bid")
                    cands.append(_bid_cand("降bid", sid, kid, name, cur, round(new_bid, 2), m, T, B, ok, why,
                        f"{m['clicks']}点击 0单（花费{m['spend']}）→ 降bid {int(step*100)}%",
                        f"高点击0单：{cur}→{round(new_bid,2)}（持续无效可考虑暂停）"))
            elif m["orders"] >= scale_orders and m["acos"] is not None and m["acos"] <= 0.8 * T and cur:
                ideal = (m["rpc"] or 0) * T
                new_bid = min(cur * (1 + step), ideal)
                if new_bid > cur * 1.02:
                    ok, why = guard(name, "scale_up")
                    cands.append(_bid_cand("加bid", sid, kid, name, cur, round(new_bid, 2), m, T, B, ok, why,
                        f"ACOS {m['acos']:.0%} ≤ 0.8×目标、{m['orders']}单 → 放量 +≤{int(step*100)}%（不超 RPC×目标）",
                        f"赢家放量：{cur}→{round(new_bid,2)}"))
    except LingXingError:
        pass

    # ---- 加预算：活动报表 + 预算 ----
    try:
        cr = _agg(sid, "sp_campaign_report", dates,
                  lambda r: str(r.get("campaign_id")) if r.get("campaign_id") else None,
                  capture=("campaign_id",), progress=progress, label="活动报表")
        budgets = _campaign_budgets(sid) if cr else {}
        for cid, bk in cr.items():
            m = _metrics(bk["_b"])
            info = budgets.get(cid) or {}
            bud = info.get("daily_budget")
            if not bud:
                continue
            avg_daily = m["spend"] / max(1, win)
            if avg_daily >= 0.85 * bud and m["acos"] is not None and m["acos"] <= tgt():
                ok, why = guard(info.get("name") or cid, "budget")
                new_bud = round(bud * (1 + step), 2)
                cands.append({
                    "lever": "加预算", "op_type": "campaign_budget", "sid": sid, "target_id": cid,
                    "target_name": info.get("name") or cid, "metrics": m,
                    "opt_target": tgt(), "opt_breakeven": brk(), "blocked": not ok, "block_reason": why,
                    "current": {"daily_budget": bud}, "proposed": {"daily_budget": new_bud},
                    "change_pct": round(step * 100, 1),
                    "rule": f"日均花费 {avg_daily:.1f} ≈ 打满预算 {bud}、ACOS {m['acos']:.0%} ≤ 目标 {tgt():.0%} → 预算 +{int(step*100)}%",
                    "significance": f"利用率≈{min(100, int(avg_daily/bud*100))}% ACOS {m['acos']:.0%}",
                    "rationale": f"活动预算打满且盈利（ACOS {m['acos']:.0%}≤目标 {tgt():.0%}），扩量。",
                })
    except LingXingError:
        pass

    order = {"否词": 0, "收割": 1, "降bid": 2, "加bid": 3, "加预算": 4, "错误": 9}
    cands.sort(key=lambda c: (order.get(c["lever"], 8), -(c.get("metrics", {}).get("spend") or 0)))
    return {"sid": sid, "window_days": win, "margin": margin, "target_acos": target,
            "breakeven_acos": breakeven, "note": note, "count": len(cands), "candidates": cands}


def _bid_cand(lever, sid, kid, name, cur, new_bid, m, target, breakeven, ok, why, rule, rationale):
    return {
        "lever": lever, "op_type": "keyword_bid", "sid": sid, "target_id": kid, "target_name": name,
        "metrics": m, "current": {"bid": cur}, "proposed": {"bid": new_bid},
        "opt_target": target, "opt_breakeven": breakeven, "blocked": not ok, "block_reason": why,
        "change_pct": round((new_bid - cur) / cur * 100, 1) if cur else None,
        "rule": rule, "significance": f"{m['clicks']}点击/{m['orders']}单", "rationale": rationale,
    }
