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
    """生成会话 id。

    随机段用 4 字节（32 位）而不是 2 字节：同一毫秒内连开会话时，16 位只有
    65536 个取值，取 20 个就有约 0.3% 概率撞上（生日问题）。而 id 直接当文件名，
    撞了就是两个会话互相覆盖 —— 这种错极少发生、发生了又很难查。
    """
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    millis = int((now % 1) * 1000)
    return f"{stamp}-{millis:03d}-{secrets.token_hex(4)}"


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
_TURN_TIMES_MAX = 2000
_SKILL_MATCH_MAX = 200


def save(sid: str, messages: list[dict], *, model: str = "", usage: Optional[dict] = None,
         created: Optional[float] = None, origin: Optional[str] = None,
         cwd: Optional[str] = None) -> None:
    with _lock_for(sid):
        _save(sid, messages, model=model, usage=usage, created=created,
              origin=origin, cwd=cwd)


def _save(sid: str, messages: list[dict], *, model: str = "", usage: Optional[dict] = None,
          created: Optional[float] = None, steps: Optional[list[dict]] = None,
          skill_matches: Optional[list[dict]] = None,
          stats: Optional[dict] = None,
          turn_times: Optional[list[dict]] = None,
          origin: Optional[str] = None, cwd: Optional[str] = None) -> None:
    p = path_for(sid)
    # steps/skill_matches/stats/turn_times/origin/cwd 没传时**沿用盘上那份**，不能当成"清空"：
    # `save()` 是整份覆盖语义（CLI 每轮就这么写），它不知道也不关心这些，但不该顺手把它们抹掉。
    #
    # origin/cwd 尤其必须在这份名单里。一条会话会被**两个写入方**轮流整份覆盖
    # （CLI 的 _persist 每轮 save 一次，serve 的收尾走 append_turn），谁都只带自己
    # 关心的字段。漏掉的话就是：serve 写下的 origin 被 CLI 的下一轮抹掉、反之亦然，
    # 于是左栏的「终端」标记时有时无 —— 这种"偶尔不对"最难查。
    if (steps is None or skill_matches is None or stats is None or turn_times is None
            or origin is None or cwd is None):
        prev = load(sid) or {}
        if steps is None:
            steps = list(prev.get("steps") or [])
        if skill_matches is None:
            skill_matches = list(prev.get("skill_matches") or [])
        if stats is None:
            stats = dict(prev.get("stats") or {})
        if turn_times is None:
            turn_times = list(prev.get("turn_times") or [])
        if origin is None:
            origin = str(prev.get("origin") or "")
        if cwd is None:
            cwd = str(prev.get("cwd") or "")
    data = {"id": sid, "created": created or time.time(), "updated": time.time(),
            "model": model, "messages": messages, "usage": usage or {},
            # 整条会话的累计账（轮数/步数/挂钟时间/模型时间/token）。**存累计而不是
            # 逐轮**：详情按轮分页，逐轮存的话打开一条长会话就只统计得到当前这一页，
            # 而"这条会话一共花了多少"从来不是按页问的。
            "stats": dict(stats or {}),
            # 执行过程与消息平行存放，**绝不塞进 messages 里的消息 dict**：
            # 那些 dict 会原样回灌给模型 API，多一个自定义键就有被 provider 拒的风险。
            "steps": list(steps)[-_STEPS_MAX:],
            "skill_matches": list(skill_matches)[-_SKILL_MATCH_MAX:],
            # 逐轮的时间账（发问时刻/收尾时刻/挂钟毫秒），**和消息平行存**。
            # 理由同 steps：messages 里的 dict 会原样回灌给 provider，多一个自定义键
            # 就有被拒的风险。界面靠它显示"发送于 09:46 / 结束于 09:49 · 用时 3 分"，
            # 刷新和换台机器打开也还在（此前这些数只活在发起它的那个页面内存里）。
            "turn_times": list(turn_times)[-_TURN_TIMES_MAX:],
            # 这条会话是从哪儿开的（"cli" / "serve"），以及开它时人在哪个目录。
            # IvyeaOps 任务台左栏靠 origin 把终端里敲的会话标成「终端」—— 在这之前
            # 它们混在列表里，没有来源、没有归属，看着像一堆无主会话。
            #
            # cwd 只是**展示用的标签**，绝不会被拿去建工作区：ops 那边给工作区绑目录
            # 是一次授权行为（agent 的文件类工具会落在那儿，仅限管理员），从 cwd
            # 自动建工作区等于静默把访问面开出去。
            "origin": str(origin or ""),
            "cwd": str(cwd or "")}
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


# 累计账里可以直接相加的字段。写死一张表而不是把 usage 整个并进去：各家 provider
# 的 usage 里什么键都可能有，无差别累加会把一堆看不懂的数糊成一笔。
_STAT_SUMS = ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "llm_ms")


def _merge_stats(prev: dict, turn: dict) -> dict:
    """把这一轮的账并进整会话的累计账。

    缺的项就不加 —— 补一个 0 等于替 provider 断言"这轮没花"，而真相是"没测到"。
    """
    out = dict(prev or {})
    out["turns"] = int(out.get("turns") or 0) + 1
    for key, val in (("steps", turn.get("steps")), ("elapsed_ms", turn.get("ms"))):
        if isinstance(val, (int, float)) and val >= 0:
            out[key] = int(out.get(key) or 0) + int(val)
    usage = turn.get("usage") or {}
    if isinstance(usage, dict):
        acc = dict(out.get("usage") or {})
        for key in _STAT_SUMS:
            val = usage.get(key)
            if isinstance(val, (int, float)):
                acc[key] = int(acc.get(key) or 0) + int(val)
        if acc:
            out["usage"] = acc
    return out


def _visible_user_count(messages: list[dict]) -> int:
    """这份消息里有几条**真实的用户提问**（口径与 transcript.turn_slices 一致）。

    逐轮时间账要挂在"第几轮"上，而轮的定义就是"第几条真实用户消息"。用下标或
    "落盘过几批"都对不上：压缩过、导入过、跨进程交错写过的会话都会错位。
    """
    return sum(1 for m in transcript.strip_injected(messages) if m.get("role") == "user")


def current_turn_index(sid: str) -> int:
    """磁盘上最后一条真实用户消息是第几轮（0 起）。没有则 -1。"""
    with _lock_for(sid):
        data = load(sid) or {}
        return _visible_user_count(list(data.get("messages") or [])) - 1


def note_turn_time(sid: str, turn: int, *, started_at: Optional[float] = None,
                   ended_at: Optional[float] = None, ms: Optional[int] = None) -> None:
    """记/更新第 `turn` 轮的时间账（upsert，只覆盖显式传进来的字段）。

    分两次写是刻意的：**开跑时**就把 started_at 落下（那时用户那句话已经落盘了，
    但这一轮还要跑几十分钟），**收尾时**再补 ended_at/ms。中途断电/进程被杀时，
    盘上至少留着"这一轮什么时候开始的"，而不是什么都没有。
    """
    if turn < 0:
        return
    with _lock_for(sid):
        data = load(sid)
        if not data:
            return
        times = list(data.get("turn_times") or [])
        row = next((t for t in times if int(t.get("turn", -1)) == int(turn)), None)
        if row is None:
            row = {"turn": int(turn), "started_at": 0.0, "ended_at": 0.0, "ms": 0}
            times.append(row)
        if started_at is not None:
            row["started_at"] = float(started_at)
        if ended_at is not None:
            row["ended_at"] = float(ended_at)
        if ms is not None:
            row["ms"] = int(ms)
        times.sort(key=lambda t: int(t.get("turn", 0)))
        _save(sid, list(data.get("messages") or []), model=str(data.get("model") or ""),
              usage=data.get("usage") or {}, created=data.get("created"),
              steps=list(data.get("steps") or []),
              skill_matches=list(data.get("skill_matches") or []),
              stats=dict(data.get("stats") or {}), turn_times=times)


def turn_times(sid: str) -> list[dict]:
    data = load(sid) or {}
    return list(data.get("turn_times") or [])


def append_turn(sid: str, system: str, new_messages: list[dict], *, model: str = "",
                usage: Optional[dict] = None, created: Optional[float] = None,
                steps: Optional[list[dict]] = None,
                skill_matches: Optional[list[dict]] = None,
                turn_stat: Optional[dict] = None) -> None:
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
              skill_matches=list(cur.get("skill_matches") or []) + list(skill_matches or []),
              stats=(_merge_stats(cur.get("stats") or {}, turn_stat)
                     if turn_stat else dict(cur.get("stats") or {})))


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
                        "preview": (first_user or "")[:50],
                        # 来源与起始目录：ops 左栏据此把终端会话标成「终端」。
                        # 老会话文件里没有这两个键，取空串 —— 消费方按"空=未知"处理。
                        "origin": str(d.get("origin") or ""),
                        "cwd": str(d.get("cwd") or "")})
        except Exception:
            pass
    return out
