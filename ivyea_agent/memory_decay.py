"""遗忘与容量治理：让记忆越用越薄，而不是越攒越厚。

**为什么必须有**：不做遗忘的记忆系统只会单调增长。前两个月体验飞升（记忆从零到有），
第三到六个月是真正的考验——记忆攒到几百上千条、有些开始过时、索引层撞上限开始截断，
而截断规则如果只按时间排，一条你每周都在用的核心打法会被一条半年前的一次性结论挤掉。

人的记忆不是这样的：常用的越记越牢，不用的自然淡去。所以打分要同时看三件事——
**用得多不多、最近用没用过、本身可不可信**。

**绝不静默删除**。低分记忆降级到"归档区"：不再常驻上下文，但检索仍能找到。
删除是不可逆的，而判断"这条没用了"本身就可能判错——降级判错的代价是少看一眼，
删除判错的代价是永久丢失。这个不对称决定了设计。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config

# 半衰期（天）：多久没被召回，"最近使用"这一项的分就掉一半。
# 45 天≈一个半月，对应运营场景里一个完整的旺季/淡季切换周期——
# 一整个周期没用上的打法，确实该让位给更活跃的。
HALFLIFE_DAYS = 45.0

# 归档线：总分低于它就不再进索引层（仍可检索）。
ARCHIVE_BELOW = 0.25

# 新记忆的保护期（天）：刚记下来还没机会被召回，不能因为"从没用过"就判低分。
GRACE_DAYS = 14.0


def db_path():
    return config.IVYEA_DIR / "memory.db"


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    from . import memory_lock
    memory_lock.tune(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS mem_usage (
        key        TEXT PRIMARY KEY,
        hits       INTEGER NOT NULL DEFAULT 0,
        last_hit   REAL,
        first_seen REAL,
        pinned     INTEGER NOT NULL DEFAULT 0)""")
    return conn


def _key(category: str, name: str) -> str:
    return f"{category}/{name}"


def record_hits(entries: List[Any]) -> None:
    """记一次召回。检索路径上每次调用，所以要便宜——一条 UPSERT，不做别的。

    统计的是"被检索命中"而不是"被模型读了正文"：后者拿不到（模型读没读我们不知道），
    前者虽然粗糙但方向正确——被反复检索到的记忆确实更相关。
    """
    if not entries:
        return
    now = time.time()
    try:
        conn = _conn()
        for e in entries:
            k = _key(getattr(e, "category", ""), getattr(e, "name", ""))
            conn.execute(
                "INSERT INTO mem_usage (key, hits, last_hit, first_seen) VALUES (?,1,?,?) "
                "ON CONFLICT(key) DO UPDATE SET hits = hits + 1, last_hit = excluded.last_hit",
                (k, now, now))
        conn.commit()
        conn.close()
    except Exception:   # noqa: BLE001 —— 统计失败绝不能影响检索本身
        pass


def usage(category: str = "", name: str = "") -> Dict[str, Any]:
    conn = _conn()
    try:
        if name:
            row = conn.execute("SELECT * FROM mem_usage WHERE key=?",
                               (_key(category, name),)).fetchone()
            return dict(row) if row else {}
        return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM mem_usage").fetchall()}
    finally:
        conn.close()


def set_pinned(category: str, name: str, pinned: bool = True) -> None:
    """钉住：永不降级。给那些"很少被检索到、但一旦用到就至关重要"的记忆用，
    比如红线规则——它们平时不会被问起，可正因如此才更不能忘。"""
    conn = _conn()
    conn.execute(
        "INSERT INTO mem_usage (key, hits, last_hit, first_seen, pinned) VALUES (?,0,NULL,?,?) "
        "ON CONFLICT(key) DO UPDATE SET pinned = excluded.pinned",
        (_key(category, name), time.time(), 1 if pinned else 0))
    conn.commit()
    conn.close()


def _recency(last_hit: Optional[float], now: float) -> float:
    """指数衰减，0~1。从没被召回过按 0 算（新记忆由保护期兜底，不在这里特殊照顾）。"""
    if not last_hit:
        return 0.0
    days = max(0.0, (now - last_hit) / 86400.0)
    return 0.5 ** (days / HALFLIFE_DAYS)


def _frequency(hits: int) -> float:
    """命中次数 → 0~1，对数压缩。第 1 次到第 5 次的差别，远比第 50 次到第 55 次重要。"""
    if hits <= 0:
        return 0.0
    import math
    return min(1.0, math.log1p(hits) / math.log1p(20))


def score(entry: Any, stats: Dict[str, Any], now: float = 0.0) -> Dict[str, Any]:
    """给一条记忆打分。返回明细而不只是数字——遗忘是会误伤的操作，
    必须能解释"为什么这条被降级了"，否则用户只会觉得 agent 莫名其妙忘事。
    """
    now = now or time.time()
    k = _key(entry.category, entry.name)
    row = stats.get(k) or {}
    hits = int(row.get("hits") or 0)
    last_hit = row.get("last_hit")
    pinned = bool(row.get("pinned"))

    freq = _frequency(hits)
    rec = _recency(last_hit, now)
    conf = float(getattr(entry, "confidence", 1.0) or 1.0)

    # 分数 = 使用度 × 可信度调制。
    #
    # 置信度做**乘数**而不是加权项：早先写成 `0.45*rec + 0.35*freq + 0.20*conf` 时，
    # 一条满置信的记忆光靠 0.20×1.0 就几乎顶到归档线，等于**永远归档不掉**——
    # 遗忘机制形同虚设。乘数形式下，没人用就是没人用，再可信也会淡出。
    #
    # 调制区间取 [0.5, 1.0] 而不是 [0, 1]：低置信只是"打个折"，不该让一条
    # 天天用得上的推断被判死——它是不是猜的，和它有没有用，是两件事。
    usage_score = 0.6 * rec + 0.4 * freq
    total = usage_score * (0.5 + 0.5 * conf)

    fresh = False
    first_seen = row.get("first_seen")
    if not first_seen:
        # 没有使用记录的老记忆：用 created 判断是不是新的
        created = getattr(entry, "created", "")
        fresh = bool(created and created >= time.strftime(
            "%Y-%m-%d", time.localtime(now - GRACE_DAYS * 86400)))
    else:
        fresh = (now - float(first_seen)) < GRACE_DAYS * 86400

    keep = pinned or fresh or total >= ARCHIVE_BELOW
    reason = ("钉住" if pinned else "保护期内" if fresh
              else "活跃" if total >= ARCHIVE_BELOW else "冷门，已降级")
    return {"key": k, "score": round(total, 4), "hits": hits, "pinned": pinned,
            "fresh": fresh, "recency": round(rec, 4), "frequency": round(freq, 4),
            "confidence": conf, "keep": keep, "reason": reason}


def rank(entries: List[Any], now: float = 0.0) -> List[Tuple[Any, Dict[str, Any]]]:
    """给全部记忆打分并按分降序。索引层据此决定谁常驻上下文。"""
    stats = usage()
    now = now or time.time()
    scored = [(e, score(e, stats, now)) for e in entries]
    # 保留的排前面；同为保留则按分数；再同分按名字，保证结果稳定可复现
    scored.sort(key=lambda t: (not t[1]["keep"], -t[1]["score"], t[0].name))
    return scored


def report(entries: List[Any]) -> Dict[str, Any]:
    ranked = rank(entries)
    archived = [(e, s) for e, s in ranked if not s["keep"]]
    return {"total": len(ranked), "active": len(ranked) - len(archived),
            "archived": len(archived),
            "halflife_days": HALFLIFE_DAYS, "archive_below": ARCHIVE_BELOW,
            "rows": [{"name": e.name, "category": e.category, **s} for e, s in ranked]}
