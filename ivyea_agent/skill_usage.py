"""技能使用统计：哪些技能真在用，哪些是死的。

为什么要有它
------------
`skills.audit()` / `status()` 能看出技能**写得**合不合格，看不出它**有没有人用**。
没有这份数据，"该归档哪条""哪两条重复了"就只能靠拍脑袋 —— 而技能库一旦长到几十上百条
（IvyeaOps 的 Skill 中心已经近百条），拍脑袋就等于不管。

记什么
------
一条技能一行：命中次数、最近一次命中的时间、最近几次命中的查询。
**不记查询全文**，只记截断后的一小段 —— 这份文件是给人翻的，不是日志。

落 `~/.ivyea/skills/usage.json`（整体读写的小文件，与 `reliability.json` 同款）。
写失败一律吞掉：统计坏了不该连累检索。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import config, security

#: 每条技能保留最近几条命中查询。够看出"它到底是被什么问题召来的"就行。
MAX_QUERIES = 5
_QUERY_CHARS = 60

_LOCK = threading.Lock()


def usage_file():
    return config.IVYEA_DIR / "skills" / "usage.json"


def _load() -> dict[str, Any]:
    path = usage_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}      # 损坏当作没有：统计不值得为它报错
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    path = usage_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return


def record(skill_ids: Any, *, query: str = "", source: str = "inject") -> None:
    """记一次命中。`source`：inject（自动注入）/ view（被读全文）/ run（被显式执行）。"""
    ids = [skill_ids] if isinstance(skill_ids, str) else list(skill_ids or [])
    ids = [str(i).strip() for i in ids if str(i).strip()]
    if not ids:
        return
    clean = " ".join(security.redact_text(str(query or "")).split())[:_QUERY_CHARS]
    now = time.time()
    with _LOCK:
        data = _load()
        for sid in ids:
            row = data.get(sid) or {}
            row["hits"] = int(row.get("hits") or 0) + 1
            row["last_at"] = now
            by_source = dict(row.get("by_source") or {})
            by_source[source] = int(by_source.get(source) or 0) + 1
            row["by_source"] = by_source
            if clean:
                queries = [q for q in (row.get("queries") or []) if q != clean]
                queries.append(clean)
                row["queries"] = queries[-MAX_QUERIES:]
            row.setdefault("first_at", now)
            data[sid] = row
        _save(data)


def stats(skill_id: str = "") -> dict[str, Any]:
    data = _load()
    return data.get(skill_id, {}) if skill_id else data


def dormant(skill_ids: Any, *, days: int = 60) -> list[str]:
    """一直没被命中过、或最近 N 天没命中过的技能 id。策展的输入。"""
    data = _load()
    cutoff = time.time() - max(1, int(days)) * 86400
    out: list[str] = []
    for sid in skill_ids or []:
        row = data.get(str(sid)) or {}
        if not row.get("hits") or float(row.get("last_at") or 0) < cutoff:
            out.append(str(sid))
    return out


def render(skill_ids: Any = None) -> str:
    data = _load()
    ids = list(skill_ids) if skill_ids is not None else sorted(data)
    if not ids:
        return "（还没有技能命中记录）"
    rows = []
    for sid in ids:
        row = data.get(str(sid)) or {}
        hits = int(row.get("hits") or 0)
        last = row.get("last_at")
        when = time.strftime("%Y-%m-%d", time.localtime(last)) if last else "-"
        by = row.get("by_source") or {}
        detail = " ".join(f"{k}={v}" for k, v in sorted(by.items())) or "-"
        rows.append((hits, f"{str(sid):<40} 命中 {hits:<5} 最近 {when:<12} {detail}"))
    rows.sort(key=lambda r: -r[0])
    return "\n".join(line for _h, line in rows)
