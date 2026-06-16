"""审计与回滚 —— 每次写操作落账，可回滚。

记录到 ~/.ivyea/audit.jsonl（每行一条）。回滚靠记录里的 before 值 + 反向写动作。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from . import config

AUDIT_FILE = config.IVYEA_DIR / "audit.jsonl"


def record(entry: dict[str, Any]) -> str:
    config.ensure_dirs()
    entry = dict(entry)
    entry.setdefault("id", uuid.uuid4().hex[:12])
    entry.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    with AUDIT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry["id"]


def load_all() -> list[dict[str, Any]]:
    if not AUDIT_FILE.exists():
        return []
    out = []
    for line in AUDIT_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def get(entry_id: str) -> Optional[dict[str, Any]]:
    for e in load_all():
        if e.get("id") == entry_id:
            return e
    return None
