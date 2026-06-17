"""影子模式 —— 动钱前先用数据换信任（垂直 agent 的护城河，三大产品没有）。

每次巡检把建议记进影子台账（记建议时的触发指标），过些天用**后续真实花费**回测：
- 否词：建议否的搜索词，之后还烧了多少钱(0单) = 若当时照做能省的钱。
- 收割：建议升精准的搜索词，之后又出了多少单 = 若当时照做能抓的增量。
新用户先开影子模式只看不写，攒够"若照做的收益"再决定要不要让它真动手。

存 ~/.ivyea/shadow.db。回测的数学是纯函数(evaluate)，便于测试；CLI 拉现况喂给它。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import config

_DB = config.IVYEA_DIR / "shadow.db"
_DEDUP_DAYS = 7


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    c = sqlite3.connect(str(_DB))
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS recs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sid TEXT, lever TEXT, target TEXT, campaign_id TEXT,
        clicks REAL, spend REAL, orders REAL, acos REAL, rule TEXT, ts REAL)""")
    return c


def record(sid: Any, candidates: list[dict]) -> int:
    """把一次巡检的候选记进台账。同 sid+lever+target 在 _DEDUP_DAYS 内不重复记。返回新增条数。"""
    sid = str(sid)
    conn = _conn()
    now = time.time()
    cutoff = now - _DEDUP_DAYS * 86400
    added = 0
    for c in candidates:
        lever = c.get("lever")
        if lever in (None, "错误"):
            continue
        target = str(c.get("target_name") or "")
        if not target:
            continue
        dup = conn.execute("SELECT 1 FROM recs WHERE sid=? AND lever=? AND target=? AND ts>? LIMIT 1",
                           (sid, lever, target, cutoff)).fetchone()
        if dup:
            continue
        m = c.get("metrics") or {}
        acos = m.get("acos")
        conn.execute("INSERT INTO recs (sid,lever,target,campaign_id,clicks,spend,orders,acos,rule,ts) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (sid, lever, target, str(c.get("campaign_id") or ""),
                      float(m.get("clicks") or 0), float(m.get("spend") or 0), float(m.get("orders") or 0),
                      float(acos) if isinstance(acos, (int, float)) else None, c.get("rule", ""), now))
        added += 1
    conn.commit(); conn.close()
    return added


def list_recs(sid: str = "", limit: int = 50) -> list[dict]:
    conn = _conn()
    if sid:
        rows = conn.execute("SELECT * FROM recs WHERE sid=? ORDER BY ts DESC LIMIT ?", (str(sid), limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM recs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def evaluate(recs: list[dict], current_terms: dict[str, dict]) -> dict[str, Any]:
    """纯函数回测。current_terms: {搜索词: {spend, orders}} —— 建议**之后**的真实表现。

    - 否词：若当时否了，之后这词的花费就省了 → saved += 后续 spend（仍 0 单才算纯浪费）。
    - 收割：若当时升精准，之后这词的单就抓住了 → harvested += 后续 orders。
    """
    saved = 0.0
    saved_terms = 0
    harvested_orders = 0.0
    harvest_terms = 0
    details = []
    for r in recs:
        cur = current_terms.get(r.get("target") or "")
        if not cur:
            continue
        if r.get("lever") == "否词":
            after_spend = float(cur.get("spend") or 0)
            after_orders = float(cur.get("orders") or 0)
            if after_spend > 0 and after_orders == 0:   # 仍是纯无效花费
                saved += after_spend
                saved_terms += 1
                details.append(f"否词「{r['target']}」之后又烧 ¥{after_spend:.2f}(0单) → 若照做已省")
        elif r.get("lever") == "收割":
            after_orders = float(cur.get("orders") or 0)
            if after_orders > 0:
                harvested_orders += after_orders
                harvest_terms += 1
                details.append(f"收割「{r['target']}」之后又出 {after_orders:.0f} 单 → 若照做已抓住")
    return {"saved_cny": round(saved, 2), "saved_terms": saved_terms,
            "harvested_orders": int(harvested_orders), "harvest_terms": harvest_terms,
            "evaluated": len([r for r in recs if r.get("target") in current_terms]),
            "details": details}


def summary_text(sid: str, result: dict) -> str:
    lines = [f"# 影子模式信任报告 — sid {sid}",
             f"> 回测了 {result['evaluated']} 条有后续数据的建议。"]
    if result["saved_terms"]:
        lines.append(f"- 否词：{result['saved_terms']} 个词若当时照做，至今**已省 ¥{result['saved_cny']:.2f}**（之后仍纯烧钱0单）。")
    if result["harvest_terms"]:
        lines.append(f"- 收割：{result['harvest_terms']} 个词若当时升精准，至今**多抓 {result['harvested_orders']} 单**。")
    if not result["saved_terms"] and not result["harvest_terms"]:
        lines.append("- 暂无可量化收益（建议太新、或后续无数据）。攒几天再看。")
    for d in result["details"][:12]:
        lines.append(f"  · {d}")
    lines.append(f"\n{'（影子模式开：只记不写，用数据换信任。`ivyea shadow off` 关。）' if config.get_setting('shadow_mode', False) else '（影子模式关。`ivyea shadow on` 开 → 只记不写。）'}")
    return "\n".join(lines)


def shadow_mode() -> bool:
    return bool(config.get_setting("shadow_mode", False))


def set_shadow(on: bool) -> None:
    config.set_setting("shadow_mode", bool(on))
