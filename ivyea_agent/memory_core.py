"""核心记忆（core memory）：常驻上下文、由 agent 自己维护的两个块。

**和检索式记忆的分工**：分类记忆文件要"想起来"才用得上（先检索、再取正文），
核心记忆则**每轮都在上下文里**，不需要检索。私人助理感来自后者——它不需要
"回忆"你是谁，它一直知道。代价是每轮都占 token，所以必须严格限长。

- `USER.md`  —— 关于用户本人的长期事实：身份、偏好、说话方式、红线。
- `AGENTS.md` —— 账户运营打法与边界：目标、保护词、阈值、禁区。

两个文件早就存在、也早就每轮注入（见 `memory.load_instructions`），
本模块补上的是**让 agent 自己能改**这一环：在此之前只有用户手动编辑才能更新，
于是"我以后不要再否品牌词"这种话说完就丢，下次照犯。

写入一律走原子替换（临时文件 + os.replace），避免写到一半被 Ctrl-C 打断留下半个文件——
这两个文件每轮都要注入，损坏的代价比普通记忆大得多。
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, Tuple

from . import config

# 块名 → (文件名, 用途说明)。说明会进工具描述，模型据此决定往哪个块写。
BLOCKS: Dict[str, Tuple[str, str]] = {
    "user": ("USER.md", "关于用户本人的长期事实：身份、角色、偏好、说话/汇报方式、绝对红线。"),
    "agents": ("AGENTS.md", "账户运营打法与边界：目标 ACoS、保护词、否词/调价阈值、禁区。"),
}

# 单块上限。核心记忆**每轮**都进 system prompt，不限长就会悄悄把上下文吃光。
# 4000 字符约等于 1500-2000 token，两块加起来仍在 load_instructions 的 6000 字符预算内。
MAX_BLOCK_CHARS = 4000

# 超过这个比例就提醒模型该整理了（而不是等撞上限硬失败）。
CROWDED_RATIO = 0.8

_SEED = {
    "user": "# 用户画像（USER.md）\n\n> Ivyea Agent 每轮都会读这个文件。写长期为真的事实，不写一次性任务。\n\n",
    "agents": "# 账户运营指令（AGENTS.md）\n\n> Ivyea Agent 每轮都会读这个文件。写希望它长期遵守的打法与边界。\n\n",
}


def block_path(block: str) -> Path:
    """块名 → 文件路径。未知块名抛 KeyError，由调用方转成给模型看的提示。"""
    name, _ = BLOCKS[block]
    return config.IVYEA_DIR / name


def _atomic_write(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再 os.replace。同目录是必须的——跨文件系统 replace 不原子。"""
    config.ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    # encoding 显式写死 utf-8：Windows 默认 GBK，写中文会炸（历史踩过）。
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


# 上一次 view() 看到的样子：block -> (mtime_ns, sha1)。
#
# **为什么需要它**：核心记忆是用户会亲手编辑的文件（USER.md / AGENTS.md 就摆在
# ~/.ivyea 下，说明书也在教用户去改）。而模型的写入是"先 view 看一眼、再据此改"
# —— 中间那段时间里用户改了，模型仍按它看到的老样子去 replace/remove，
# 就会把用户刚写的东西抹掉。
#
# 只在**本进程 view 过**的前提下才判漂移：没有基线就谈不上检测，
# 那种情况（模型没看就写）本来也不会覆盖别人的内容（append 是追加、
# replace 要求在当前正文里唯一命中）。
_SEEN: Dict[str, Tuple[int, str]] = {}


def _fingerprint(path: Path) -> Tuple[int, str]:
    """(mtime_ns, 内容 sha1)。只看 mtime 不够——同一秒内的两次写会看起来没变。"""
    try:
        st = path.stat()
        data = path.read_bytes()
    except OSError:
        return (0, "")
    return (st.st_mtime_ns, hashlib.sha1(data).hexdigest())


def view(block: str) -> str:
    """读一个核心记忆块的全文（不存在则返回空串）。

    顺手记下"这次看到的是什么样"，作为后续写入的漂移基线。
    """
    p = block_path(block)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    _SEEN[block] = _fingerprint(p)
    return text


def _usage(text: str) -> Dict[str, Any]:
    used = len(text)
    return {"chars": used, "limit": MAX_BLOCK_CHARS,
            "crowded": used > MAX_BLOCK_CHARS * CROWDED_RATIO}


def edit(block: str, operation: str, content: str = "", old: str = "") -> Dict[str, Any]:
    """改一个核心记忆块。返回 {"ok", "message", ...}，不抛异常给模型。

    operation:
    - `append`  追加一条要点（自动加日期前缀，便于日后判断时效）
    - `replace` 把 `old` 原样替换成 `content`；`old` 必须**唯一命中**，否则拒绝
    - `remove`  删掉包含 `old` 的整行

    replace 要求唯一命中是刻意的：核心记忆是高价值小文本，"改错一处"比"没改成"
    代价大得多，宁可让模型先 view 一遍再精确指定。
    """
    if block not in BLOCKS:
        return {"ok": False, "message": f"未知的记忆块 {block!r}，可选：{', '.join(BLOCKS)}"}
    op = (operation or "").strip().lower()
    path = block_path(block)

    # 漂移检查必须发生在**读正文之前**，而且要拿同一次读到的指纹去比。
    # Hermes 在这里踩过一次：它在检测时又读了一遍文件，两次读之间被人改掉的话，
    # 后面的写入仍然基于第一次那份陈旧快照，照样把新内容抹掉。
    baseline = _SEEN.get(block)
    if baseline is not None:
        current = _fingerprint(path)
        if current != baseline:
            bak = _backup(path)
            _SEEN[block] = current          # 基线对齐到现状，模型重新 view 后即可写
            return {"ok": False, "drift": True, "backup": bak,
                    "message": (f"{BLOCKS[block][0]} 在你上次查看之后被**外部修改**过"
                                f"（用户手改或另一个会话写入）。本次写入已拒绝，"
                                f"当前内容已备份到 {bak or '（备份失败）'}。"
                                f"请先用 core_memory_view 重新看一遍，再基于新内容改。")}

    text = view(block) or _SEED.get(block, "")

    if op == "append":
        body = (content or "").strip()
        if not body:
            return {"ok": False, "message": "content 为空，没有可追加的内容。"}
        stamp = time.strftime("%Y-%m-%d")
        # 绝对日期而非"上周/最近"：记忆会被反复读到，相对时间过几天就是错的。
        addition = f"- [{stamp}] {body}\n"
        new_text = (text.rstrip("\n") + "\n" + addition) if text.strip() else (text + addition)

    elif op == "replace":
        if not old:
            return {"ok": False, "message": "replace 需要提供 old（要被替换的原文）。"}
        hits = text.count(old)
        if hits == 0:
            return {"ok": False, "message": f"没找到要替换的原文；先用 core_memory_view 看一眼 {block} 的当前内容。"}
        if hits > 1:
            return {"ok": False, "message": f"old 在文中命中 {hits} 处，不唯一；请带上更多上下文让它唯一。"}
        new_text = text.replace(old, content or "")

    elif op == "remove":
        if not old:
            return {"ok": False, "message": "remove 需要提供 old（要删除那一行里的特征文本）。"}
        kept = [ln for ln in text.splitlines() if old not in ln]
        if len(kept) == len(text.splitlines()):
            return {"ok": False, "message": "没有匹配的行被删除。"}
        new_text = "\n".join(kept) + "\n"

    else:
        return {"ok": False, "message": f"未知操作 {operation!r}，可选：append / replace / remove"}

    if len(new_text) > MAX_BLOCK_CHARS:
        return {"ok": False,
                "message": (f"{block} 块会超出 {MAX_BLOCK_CHARS} 字上限（当前 {len(text)} 字）。"
                            "核心记忆每轮都占上下文，请先合并或删掉过时条目，再写入。")}

    _atomic_write(path, new_text)
    # 自己写完要把基线对齐，否则下一次写入会把"我自己刚改的"当成外部漂移。
    _SEEN[block] = _fingerprint(path)
    usage = _usage(new_text)
    msg = f"已更新核心记忆 {BLOCKS[block][0]}（{usage['chars']}/{MAX_BLOCK_CHARS} 字）。下一轮起生效。"
    if usage["crowded"]:
        msg += " 提醒：该块已接近上限，建议合并同类条目。"
    return {"ok": True, "message": msg, "block": block, "path": str(path), **usage}


def _backup(path: Path) -> str:
    """漂移时给当前磁盘内容留一份 .bak.<时间戳>，让人能取回被改的版本。

    只在**真的检测到漂移**时写，不是每次写入都备份——否则 ~/.ivyea 很快堆满。
    """
    if not path.exists():
        return ""
    bak = path.with_name(f"{path.name}.bak.{int(time.time())}")
    try:
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return str(bak)
    except OSError:
        return ""


def describe() -> str:
    """给工具描述用的块清单，避免在 schema 里和 BLOCKS 定义重复维护。"""
    return "；".join(f"{k}={v[1]}" for k, v in BLOCKS.items())


def status() -> Dict[str, Any]:
    """各块当前占用，供 `ivyea memory` 展示和自检。"""
    out: Dict[str, Any] = {}
    for name in BLOCKS:
        text = view(name)
        out[name] = {"file": BLOCKS[name][0], "exists": bool(text), **_usage(text)}
    return out
