"""告警状态机 —— 只在「正常 → 异常」的那一刻报，恢复时报一条（方案 §5.3）。

没有这一层的时候，一条断货告警会**每一轮巡检重推一次**：L1 按小时跑，一个持续
一周没处理的问题就是 168 张一模一样的卡。人的反应是把这个群静音，然后真出事的
那张卡也一起看不见了 —— 告警系统失效的典型路径不是漏报，是刷屏。

四道闸里的两道在这里（另两道在别处，见下）：

1. **去重指纹**：``code + target_id + severity``。同指纹在窗口内只报一次，
   ``crit`` 的窗口短一些（4 小时 vs 24 小时）—— 紧急的事值得多提醒你一次。
2. **状态跃迁才报**：持续异常不重复报；**消失时报一条「已恢复」**。
   严重度升级（warn → crit）指纹变了，视作新事件立即报。

另两道：批量合并在 ``store_health.collapse_variants``（同母体变体合一条）；
静默时段尚未实现。

**最危险的一条规矩：取数失败时绝不判恢复。**
规则没跑 ≠ 问题没了。数据源一挂，findings 天然为空，若照常判恢复，
你会在断货最严重的那天收到一屏「✅ 已恢复」。所以 ``triage`` 只在本层
**跑干净了**（无数据缺口）时才计算恢复项。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import config

_FILE = config.IVYEA_DIR / "alert_state.json"

#: 同一指纹的重复提醒间隔（秒）。crit 短一些。
REMIND_SECONDS = {"crit": 4 * 3600.0, "warn": 24 * 3600.0, "info": 24 * 3600.0}


def _load() -> dict[str, Any]:
    if not _FILE.exists():
        return {}
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001 —— 坏了当空的
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    config.ensure_dirs()
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_FILE)                                  # 原子替换，避免写一半被读到


def fingerprint(finding: Any) -> str:
    basis = "|".join([
        str(getattr(finding, "code", "")),
        str(getattr(finding, "target_id", "")),
        str(getattr(finding, "severity", "")),
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _key(sid: Any, layer: str, finding: Any) -> str:
    return f"{sid}|{layer}|{getattr(finding, 'code', '')}|{getattr(finding, 'target_id', '')}"


@dataclass
class Triage:
    """本轮该推什么。

    ``fresh`` 才进卡片和审批项；``ongoing`` 只是让你知道它还在，不打扰；
    ``resolved`` 各推一行「已恢复」。
    """
    fresh: list[Any] = field(default_factory=list)
    ongoing: list[Any] = field(default_factory=list)
    resolved: list[dict[str, Any]] = field(default_factory=list)

    @property
    def should_push(self) -> bool:
        return bool(self.fresh or self.resolved)


def triage(sid: Any, layer: str, findings: Iterable[Any], *,
           clean: bool = True, now: Optional[float] = None) -> Triage:
    """按状态机分流本轮 findings，并落盘新状态。

    ``clean=False``（本层有数据缺口）时**不判恢复**，理由见模块开头。
    """
    now = now or time.time()
    state = _load()
    out = Triage()
    seen: set[str] = set()

    for f in findings:
        key = _key(sid, layer, f)
        seen.add(key)
        fp = fingerprint(f)
        prev = state.get(key)
        sev = str(getattr(f, "severity", "info"))
        window = REMIND_SECONDS.get(sev, 24 * 3600.0)

        if prev and prev.get("fp") == fp and (now - float(prev.get("last_alert") or 0)) < window:
            out.ongoing.append(f)
            prev["last_seen"] = now
            continue

        out.fresh.append(f)
        state[key] = {
            "fp": fp, "sid": str(sid), "layer": layer,
            "code": str(getattr(f, "code", "")),
            "target_id": str(getattr(f, "target_id", "")),
            "target_name": str(getattr(f, "target_name", "")),
            "severity": sev,
            "message": str(getattr(f, "message", "")),
            "first_seen": float((prev or {}).get("first_seen") or now),
            "last_seen": now,
            "last_alert": now,
        }

    if clean:
        for key, rec in list(state.items()):
            if key in seen:
                continue
            if str(rec.get("sid")) != str(sid) or str(rec.get("layer")) != layer:
                continue
            out.resolved.append({**rec, "resolved_at": now})
            state.pop(key, None)

    _save(state)
    return out


def active(sid: Any = "", layer: str = "") -> list[dict[str, Any]]:
    """当前仍在的告警（周报/月报的「还没解决」用它）。"""
    rows = []
    for rec in _load().values():
        if sid and str(rec.get("sid")) != str(sid):
            continue
        if layer and str(rec.get("layer")) != layer:
            continue
        rows.append(rec)
    return sorted(rows, key=lambda r: float(r.get("first_seen") or 0))


def forget(sid: Any = "") -> int:
    """清空状态（换店铺、改阈值后想重新报一遍时用）。"""
    state = _load()
    if not sid:
        n = len(state)
        _save({})
        return n
    keys = [k for k, v in state.items() if str(v.get("sid")) == str(sid)]
    for k in keys:
        state.pop(k, None)
    _save(state)
    return len(keys)
