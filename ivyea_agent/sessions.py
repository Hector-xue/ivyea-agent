"""会话持久化与 resume（对标 Claude Code --resume/--continue）。

每个会话存 ~/.ivyea/sessions/<id>.json：{id, created, updated, model, messages, usage}。
每轮对话后落盘；`ivyea chat --resume` 续最近一个，`--resume <id>` 续指定。
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from . import config

_DIR = config.IVYEA_DIR / "sessions"


def _dir() -> Path:
    config.ensure_dirs()
    _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR


def new_id() -> str:
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    millis = int((now % 1) * 1000)
    return f"{stamp}-{millis:03d}-{secrets.token_hex(2)}"


def path_for(sid: str) -> Path:
    return _dir() / f"{sid}.json"


def save(sid: str, messages: list[dict], *, model: str = "", usage: Optional[dict] = None,
         created: Optional[float] = None) -> None:
    p = path_for(sid)
    data = {"id": sid, "created": created or time.time(), "updated": time.time(),
            "model": model, "messages": messages, "usage": usage or {}}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def load(sid: str) -> Optional[dict[str, Any]]:
    p = path_for(sid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def latest_id() -> Optional[str]:
    files = sorted(_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def delete(sid: str) -> bool:
    """Delete one persisted session file. Returns True if a file was removed.
    Guards against path traversal — only deletes inside the sessions dir."""
    if not sid:
        return False
    p = path_for(sid)
    try:
        if p.resolve().parent != _dir().resolve():
            return False
        if p.exists():
            p.unlink()
            return True
    except Exception:
        pass
    return False


def listing(limit: int = 20) -> list[dict[str, Any]]:
    out = []
    files = sorted(_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[:limit]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            msgs = d.get("messages", [])
            first_user = next((m.get("content", "") for m in msgs if m.get("role") == "user"), "")
            out.append({"id": d.get("id", f.stem), "updated": d.get("updated"),
                        "turns": sum(1 for m in msgs if m.get("role") == "user"),
                        "preview": (first_user or "")[:50]})
        except Exception:
            pass
    return out
