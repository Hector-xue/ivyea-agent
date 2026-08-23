"""日内采样 —— L2 层的基础设施。

领星（以及亚马逊 Ads 报表 API）只给天粒度。但传 ``report_date=今天`` 拿到的是
**当日累计值**：每小时采一次样、与上一次做差，就得到自制的小时增量。
这是在不改接口的前提下把广告监控从 T+1 压到小时级的办法。

接入 Amazon Marketing Stream 后这一层会被推送取代（它直接给小时数据），
但规则不变——规则读的是「小时增量」这个概念，不是采样实现（ADR-8）。

存 ~/.ivyea/intraday.db。

三条纪律：
1. **跨日不做差**：新的一天累计值归零，与昨天末次采样相减会得到大负数。
2. **负增量视为数据修正**：亚马逊会回溯调整当日数据，出现负值时归零并标注，
   绝不能把"花费变成 -50"当成异常报出去。
3. **同一天的首次采样没有增量**：不报，只记录。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import config

_DB = config.IVYEA_DIR / "intraday.db"

#: 参与差分的累计字段
CUMULATIVE_FIELDS = ("impressions", "clicks", "spend", "orders", "sales")


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    c = sqlite3.connect(str(_DB))
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS samples (
        sid TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
        day TEXT NOT NULL, ts REAL NOT NULL,
        impressions REAL DEFAULT 0, clicks REAL DEFAULT 0, spend REAL DEFAULT 0,
        orders REAL DEFAULT 0, sales REAL DEFAULT 0)""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_samples_lookup
        ON samples (sid, entity, entity_id, day, ts)""")
    return c


@dataclass
class Delta:
    """两次采样之间的增量。"""
    entity_id: str
    day: str
    seconds: float
    values: dict[str, float] = field(default_factory=dict)
    corrected: bool = False          # 出现负增量并被归零
    cumulative: dict[str, float] = field(default_factory=dict)

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    def per_hour(self, key: str) -> float:
        h = self.hours
        return (self.values.get(key, 0.0) / h) if h > 0 else 0.0


@dataclass
class SampleResult:
    day: str
    deltas: list[Delta] = field(default_factory=list)
    first_sample_of_day: bool = False
    rows_recorded: int = 0
    #: U8 观测：本次是否观察到任何累计值上涨。全为 0 且有活跃投放时，
    #: 说明该数据源当日数据不滚动，L2 需改由推送源承载。
    observed_growth: bool = False


def record_and_diff(sid: Any, entity: str, day: str,
                    rows: Iterable[dict[str, Any]], key_field: str) -> SampleResult:
    """记一次采样并与**同一天**的上一次采样做差。"""
    sid = str(sid)
    now = time.time()
    rows = [r for r in rows if str(r.get(key_field) or "")]
    res = SampleResult(day=day)

    conn = _conn()
    try:
        prev: dict[str, sqlite3.Row] = {}
        for r in conn.execute(
                """SELECT s.* FROM samples s
                   JOIN (SELECT entity_id, MAX(ts) AS mts FROM samples
                         WHERE sid=? AND entity=? AND day=? GROUP BY entity_id) m
                     ON s.entity_id=m.entity_id AND s.ts=m.mts
                   WHERE s.sid=? AND s.entity=? AND s.day=?""",
                (sid, entity, day, sid, entity, day)):
            prev[r["entity_id"]] = r

        res.first_sample_of_day = not prev

        payload = []
        for r in rows:
            eid = str(r.get(key_field))
            cur = {f: float(r.get(f) or 0) for f in CUMULATIVE_FIELDS}
            payload.append((sid, entity, eid, day, now, cur["impressions"],
                            cur["clicks"], cur["spend"], cur["orders"], cur["sales"]))
            p = prev.get(eid)
            if p is None:
                continue
            gap = now - float(p["ts"])
            if gap <= 0:
                continue
            values, corrected = {}, False
            for f in CUMULATIVE_FIELDS:
                d = cur[f] - float(p[f] or 0)
                if d < 0:
                    # 亚马逊会回溯修正当日数据；负增量不是异常，是修正
                    corrected = True
                    d = 0.0
                values[f] = d
            if any(v > 0 for v in values.values()):
                res.observed_growth = True
            res.deltas.append(Delta(entity_id=eid, day=day, seconds=gap,
                                    values=values, corrected=corrected,
                                    cumulative=cur))

        conn.executemany(
            """INSERT INTO samples
               (sid,entity,entity_id,day,ts,impressions,clicks,spend,orders,sales)
               VALUES (?,?,?,?,?,?,?,?,?,?)""", payload)
        conn.commit()
        res.rows_recorded = len(payload)
    finally:
        conn.close()
    return res


def rate_baseline(sid: Any, entity: str, entity_id: str, hour: int,
                  *, exclude_day: str = "", min_days: int = 3,
                  tolerance_hours: int = 3) -> Optional[dict[str, float]]:
    """同时段历史基线，单位是**每小时速率**。

    两个必须记住的点：

    1. **返回速率，不是区间增量。** 老版本返回的是"两次采样之间的差值"，
       在每小时采一次样时它恰好等于小时速率，于是调用方拿它和 ``per_hour()``
       比看着没毛病。采样间隔一改（比如 12 小时一轮），基线变成 12 小时的总量、
       当前值仍是每小时速率，一比就是 12 倍差 —— 规则**永远不会触发**，
       而且不会报错，静默失效。速率化之后，这条规则与采样节奏解耦。
    2. **同时段是带容差的。** 任务由「上次跑完 + 间隔」调度，每天的执行时刻会
       随耗时缓慢漂移；要求整点严格相等，漂过一个小时边界基线就凭空消失。

    数据不足 ``min_days`` 天时返回 None —— 调用方应改用配速兜底，
    而不是拿一天的数据当基线（那样第二天就会满屏告警）。
    """
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT day, ts, impressions, clicks, spend, orders, sales
               FROM samples WHERE sid=? AND entity=? AND entity_id=? AND day<>?
               ORDER BY day, ts""",
            (str(sid), entity, entity_id, exclude_day)).fetchall()
    finally:
        conn.close()

    by_day: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)

    acc: dict[str, list[float]] = {f: [] for f in CUMULATIVE_FIELDS}
    for _day, samples in by_day.items():
        # 取当天**结束时刻最接近目标时段**的那一段增量
        best: Optional[tuple[int, Any, Any]] = None
        for i in range(1, len(samples)):
            end_hour = time.localtime(samples[i]["ts"]).tm_hour
            gap = min(abs(end_hour - hour), 24 - abs(end_hour - hour))
            if gap > tolerance_hours:
                continue
            if best is None or gap < best[0]:
                best = (gap, samples[i - 1], samples[i])
        if best is None:
            continue
        _gap, p, c = best
        hours = max(1e-6, (float(c["ts"]) - float(p["ts"])) / 3600.0)
        for f in CUMULATIVE_FIELDS:
            acc[f].append(max(0.0, float(c[f] or 0) - float(p[f] or 0)) / hours)

    n = len(acc["spend"])
    if n < min_days:
        return None
    return {f: (sum(v) / len(v) if v else 0.0) for f, v in acc.items()}


def prune(days: int = 14) -> int:
    """删除过老的采样。日内层只需要最近几天做基线。"""
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = _conn()
    try:
        n = conn.execute("DELETE FROM samples WHERE day < ?", (cutoff,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return n


def clear(sid: Any = "") -> int:
    conn = _conn()
    try:
        if sid:
            n = conn.execute("DELETE FROM samples WHERE sid=?", (str(sid),)).rowcount
        else:
            n = conn.execute("DELETE FROM samples").rowcount
        conn.commit()
    finally:
        conn.close()
    return n
