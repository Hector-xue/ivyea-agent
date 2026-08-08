"""会话持久化与 resume（对标 Claude Code --resume/--continue）。

每个会话存 ~/.ivyea/sessions/<id>.json：{id, created, updated, model, messages, usage}。
每轮对话后落盘；`ivyea chat --resume` 续最近一个，`--resume <id>` 续指定。
"""
from __future__ import annotations

import json
import re as _re
import secrets
import threading
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


# 一条会话一把锁。会话文件是**整份覆盖**写的，所以"读出来→改→写回去"这段必须
# 串起来 —— 否则两个标签页在同一会话里同时发消息，后写的那份会把先写的整轮
# （连问带答）悄悄吃掉。实测复现过：两轮都正常出了字，落盘只剩一轮。
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(sid: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(sid)
        if lock is None:
            lock = _LOCKS[sid] = threading.RLock()
        return lock


def save(sid: str, messages: list[dict], *, model: str = "", usage: Optional[dict] = None,
         created: Optional[float] = None) -> None:
    with _lock_for(sid):
        _save(sid, messages, model=model, usage=usage, created=created)


def _save(sid: str, messages: list[dict], *, model: str = "", usage: Optional[dict] = None,
          created: Optional[float] = None) -> None:
    p = path_for(sid)
    data = {"id": sid, "created": created or time.time(), "updated": time.time(),
            "model": model, "messages": messages, "usage": usage or {}}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def append_turn(sid: str, system: str, new_messages: list[dict], *, model: str = "",
                usage: Optional[dict] = None, created: Optional[float] = None) -> None:
    """把**这一轮新增的**消息并进磁盘上那份，而不是拿内存里的整份覆盖。

    为什么不能整份覆盖：一轮的流程是"开始时读全部历史 → 跑 → 结束时写回全部"。
    两个标签页同时在一条会话里发消息，各自读到的都是那一刻的历史，结束时各自写回
    自己那份 —— 后写的赢，先写的那一整轮就没了。而且**没有任何报错**，两边界面上
    都好好地出了字，只有刷新之后才会发现少了一轮。

    改成只追加增量之后，两轮都留得下来（顺序按落盘先后交错，但一条都不丢）。
    整段读改写在会话锁里，所以两个并发的收尾不会互相踩。
    """
    with _lock_for(sid):
        cur = load(sid) or {}
        msgs = list(cur.get("messages") or [])
        # system 用本轮这份：它带着当前的技能/知识注入，是这一轮的运行时上下文
        if msgs and msgs[0].get("role") == "system":
            msgs[0] = {"role": "system", "content": system}
        else:
            msgs.insert(0, {"role": "system", "content": system})
        msgs.extend(new_messages)
        _save(sid, msgs, model=model, usage=usage,
              created=cur.get("created") or created)


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
