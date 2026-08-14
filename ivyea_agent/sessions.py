"""会话持久化与 resume（对标 Claude Code --resume/--continue）。

每个会话存 ~/.ivyea/sessions/<id>.json：{id, created, updated, model, messages, usage}。
每轮对话后落盘；`ivyea chat --resume` 续最近一个，`--resume <id>` 续指定。
"""
from __future__ import annotations

import json
import os
import re as _re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import config, transcript

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

# Windows 的保留设备名。`NUL.json` 在 Windows 上**就是空设备** —— 写进去内容直接
# 消失，而且不报错；`CON` 会去开控制台。字符集守卫拦不住它们（全是合法字符），
# 所以单独列一份。带扩展名也一样算设备，所以比的是整个 id。
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def is_safe_id(sid: str) -> bool:
    if not sid or _SAFE_ID.fullmatch(sid) is None:
        return False
    # 无论当前跑在哪个系统都拒：会话文件会跟着备份/同步挪到 Windows 机器上，
    # 而且 daemon 本身就支持 Windows。只在 nt 上拦，等于放任生成一批到了
    # Windows 才炸的 id。
    return sid.upper() not in _WINDOWS_RESERVED


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


# 一条会话最多留多少步执行记录。步骤是给人复盘用的，不参与模型上下文，所以可以封顶；
# 每条经 stream_json._slim_args 裁过，2000 条约几百 KB 量级。
_STEPS_MAX = 2000
_SKILL_MATCH_MAX = 200


def save(sid: str, messages: list[dict], *, model: str = "", usage: Optional[dict] = None,
         created: Optional[float] = None) -> None:
    with _lock_for(sid):
        _save(sid, messages, model=model, usage=usage, created=created)


def _save(sid: str, messages: list[dict], *, model: str = "", usage: Optional[dict] = None,
          created: Optional[float] = None, steps: Optional[list[dict]] = None,
          skill_matches: Optional[list[dict]] = None) -> None:
    p = path_for(sid)
    # steps/skill_matches 没传时**沿用盘上那份**，不能当成"清空"：`save()` 是整份覆盖
    # 语义（CLI 每轮就这么写），它不知道也不关心步骤，但不该顺手把它们抹掉。
    if steps is None or skill_matches is None:
        prev = load(sid) or {}
        if steps is None:
            steps = list(prev.get("steps") or [])
        if skill_matches is None:
            skill_matches = list(prev.get("skill_matches") or [])
    data = {"id": sid, "created": created or time.time(), "updated": time.time(),
            "model": model, "messages": messages, "usage": usage or {},
            # 执行过程与消息平行存放，**绝不塞进 messages 里的消息 dict**：
            # 那些 dict 会原样回灌给模型 API，多一个自定义键就有被 provider 拒的风险。
            "steps": list(steps)[-_STEPS_MAX:],
            "skill_matches": list(skill_matches)[-_SKILL_MATCH_MAX:]}
    # 临时文件名带进程号和随机后缀。固定成 `<id>.json.tmp` 的话，两个**进程**同时
    # 写同一条会话（比如工作台的 serve 和一个 `ivyea chat`）会写进同一个临时文件，
    # 互相踩出半截 JSON。进程内的会话锁管不到跨进程。
    tmp = p.with_name(f"{p.stem}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # Windows 上 os.replace 会在**别的进程正开着目标文件**时抛 PermissionError
    # （POSIX 从不会）。这里的目标恰恰是会被并发读的会话文件，而 Windows 是主要
    # 用户环境 —— 不重试的话，赶上一次就是这一轮的回答没落盘。
    for attempt in range(6):
        try:
            tmp.replace(p)
            return
        except PermissionError:
            if attempt == 5:
                tmp.unlink(missing_ok=True)   # 别把半截临时文件留在会话目录里
                raise
            time.sleep(0.05 * (attempt + 1))


def append_turn(sid: str, system: str, new_messages: list[dict], *, model: str = "",
                usage: Optional[dict] = None, created: Optional[float] = None,
                steps: Optional[list[dict]] = None,
                skill_matches: Optional[list[dict]] = None) -> None:
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
        # 步骤同样只追加增量 —— 理由和消息一模一样（两个标签页并发收尾时，
        # 整份覆盖会让先写的那一轮连人带步骤一起消失）。
        _save(sid, msgs, model=model, usage=usage,
              created=cur.get("created") or created,
              steps=list(cur.get("steps") or []) + list(steps or []),
              skill_matches=list(cur.get("skill_matches") or []) + list(skill_matches or []))


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
            # 门禁提示和压缩摘要也是 role=user，直接数会把轮数虚高、还可能被当成首句摘要。
            first_user = next((m.get("content", "") for m in msgs
                               if m.get("role") == "user"
                               and not transcript.is_injected_user_message(m.get("content"))), "")
            out.append({"id": d.get("id", f.stem), "updated": d.get("updated"),
                        "turns": transcript.visible_turns(msgs),
                        "preview": (first_user or "")[:50]})
        except Exception:
            pass
    return out
