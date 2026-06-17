"""领星取数缓存（SQLite，按数据集+参数哈希 + TTL）。

巡检逐日聚合会发数十次请求(1次/秒)，慢且耗额度。缓存让重复巡检秒回、尊重限流。
历史报表(已定型)长缓存；实时状态(bid/预算)短缓存。`fetch_dataset` 是带缓存的取数入口，
签名与 lingxing_datasets.fetch_dataset 一致(2 参)，供 optimizer 直接替换。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any

from . import config
from . import lingxing_datasets as _ds

_DB = config.IVYEA_DIR / "lingxing_cache.db"

# 按数据集的 TTL（秒）。历史报表定型→长；实时状态→短。
_TTL = {
    "sp_search_term_report": 7 * 86400, "sp_keyword_report": 7 * 86400,
    "sp_campaign_report": 7 * 86400, "asin_profit": 7 * 86400,
    "sp_keywords": 1800, "sp_campaigns": 1800, "sp_product_ads": 1800,
}
_DEFAULT_TTL = 1800


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    c = sqlite3.connect(str(_DB))
    c.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, ts REAL, payload TEXT)")
    return c


def _key(name: str, params: dict) -> str:
    blob = name + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def fetch_dataset(name: str, params: dict, *, force: bool = False) -> list[dict[str, Any]]:
    """带缓存取数。命中且未过期→直接返回；否则真取→入库。force 跳过缓存。"""
    ttl = _TTL.get(name, _DEFAULT_TTL)
    k = _key(name, params)
    if not force:
        try:
            conn = _conn()
            row = conn.execute("SELECT ts, payload FROM cache WHERE k=?", (k,)).fetchone()
            conn.close()
            if row and (time.time() - row[0]) < ttl:
                return json.loads(row[1])
        except Exception:
            pass
    rows = _ds.fetch_dataset(name, params)
    try:
        conn = _conn()
        conn.execute("INSERT OR REPLACE INTO cache (k, ts, payload) VALUES (?,?,?)",
                     (k, time.time(), json.dumps(rows, ensure_ascii=False)))
        conn.commit(); conn.close()
    except Exception:
        pass
    return rows


def clear() -> int:
    """清空缓存，返回清掉的条数。"""
    try:
        conn = _conn()
        n = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        conn.execute("DELETE FROM cache"); conn.commit(); conn.close()
        return n
    except Exception:
        return 0
