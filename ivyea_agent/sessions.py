"""会话持久化与 resume（对标 Claude Code --resume/--continue）。

每个会话存 ~/.ivyea/sessions/<id>.json：{id, created, updated, model, messages, usage}。
每轮对话后落盘；`ivyea chat --resume` 续最近一个，`--resume <id>` 续指定。
"""
from __future__ import annotations

import json
import re as _re
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


# 会话 id 会直接拼进文件名，而 id 是**调用方给的**（serve 的 payload.session_id、
# 导入接口的 id），所以必须当成不可信输入。不校验的话 `../../../x` 就是一次
# 任意路径写入 —— daemon 常以 root 跑，代价是整台机器。
#
# 字符集按 new_id() 的产物取（时间戳-毫秒-随机十六进制），另外放行下划线，
# 好让外部系统的 id 迁进来。落盘的 164 个历史会话全部符合，收紧不误伤存量。
_SAFE_ID = _re.compile(r"[A-Za-z0-9_-]{1,120}")


def is_safe_id(sid: str) -> bool:
    return bool(sid) and _SAFE_ID.fullmatch(sid) is not None


def path_for(sid: str) -> Path:
    if not is_safe_id(sid):
        raise ValueError(f"unsafe session id: {sid[:40]!r}")
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
    if not is_safe_id(sid):
        return None          # 查询语义：非法 id 等同"查无此会话"，不必抛
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
    if not is_safe_id(sid):
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
