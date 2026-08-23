"""连续失败计数 —— 方案 §8 降级链的基础。

为什么要"连续"而不是"累计"：巡检每 20 分钟一次，偶发一次超时是常态，
累计计数迟早会触发，变成狼来了。只有**连续**失败才说明是真故障。

成功即清零。存 ~/.ivyea/reliability.json（小文件，直接整体读写）。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import config

_FILE = config.IVYEA_DIR / "reliability.json"
_LOCK = threading.Lock()


def _load() -> dict[str, Any]:
    if not _FILE.exists():
        return {}
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 损坏不该拖垮巡检：当作没有历史，重新计数
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    config.ensure_dirs()
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_FILE)          # 原子替换，避免写一半被读到


def record_failure(key: str, detail: str = "") -> int:
    """记一次失败，返回**连续**失败次数。"""
    with _LOCK:
        data = _load()
        row = data.get(key) or {}
        n = int(row.get("consecutive") or 0) + 1
        data[key] = {"consecutive": n, "last_failure": time.time(),
                     "detail": str(detail)[:500],
                     "first_failure": row.get("first_failure") or time.time()}
        _save(data)
    return n


def record_success(key: str) -> None:
    with _LOCK:
        data = _load()
        if key in data:
            data.pop(key, None)
            _save(data)


def count(key: str) -> int:
    return int((_load().get(key) or {}).get("consecutive") or 0)


def detail(key: str) -> str:
    return str((_load().get(key) or {}).get("detail") or "")


def should_alert(key: str, threshold: int) -> bool:
    """恰好达到阈值时返回 True —— **只在跨过门槛的那一次**报，
    之后持续失败不再重复轰炸（恢复后计数清零，下次故障会重新报）。"""
    return count(key) == int(threshold)


def snapshot() -> dict[str, Any]:
    return _load()


def clear(key: str = "") -> None:
    with _LOCK:
        if not key:
            _save({})
            return
        data = _load()
        if data.pop(key, None) is not None:
            _save(data)
