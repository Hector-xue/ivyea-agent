"""跨进程写锁 + SQLite 并发设置。

**为什么需要**：`~/.ivyea` 同时被三个进程写——CLI、本机 serve(8765)、IvyeaOps 嵌入模式。
分类记忆的写入是"先查重、再落盘"的读-改-写序列，两个进程同时跑就会双双查重通过、
各建一条近似重复的记忆（TOCTOU）。单个文件的 os.replace 是原子的，救不了这个。

锁的粒度是**整个记忆写入操作**而不是单个文件：查重要看全库，所以临界区必须覆盖
"读全库 + 写一个文件"。记忆写入本来就低频（一次对话几条），锁竞争可以忽略。

用 fcntl/msvcrt 的文件锁而不是 SQLite 事务：待定区、历史归档、分类记忆都是**文件**，
不在数据库里，SQLite 的锁管不到它们。
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Optional

from . import config

# 拿不到锁就一直等的上限。超时后**照常执行**而不是报错：
# 记忆写入失败对用户来说是"我说了记住结果没记"，比偶发的重复条目糟得多；
# 而重复条目下一次查重/反思还有机会合并。宁可脏一点，不可丢。
LOCK_TIMEOUT = 10.0


def lock_path():
    config.ensure_dirs()
    return config.IVYEA_DIR / "memory.lock"


@contextmanager
def memory_write_lock(timeout: float = LOCK_TIMEOUT):
    """跨进程互斥。拿不到就等，超时则放行并记一笔（不阻断业务）。"""
    path = str(lock_path())
    fh = None
    acquired = False
    try:
        fh = open(path, "a+")
        acquired = _acquire(fh, timeout)
        if not acquired:
            from . import log
            log.dbg("memory.lock", f"等待 {timeout}s 未获得记忆写锁，放行（可能产生重复条目）")
        yield acquired
    finally:
        if fh is not None:
            if acquired:
                _release(fh)
            try:
                fh.close()
            except OSError:
                pass


def _acquire(fh, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while True:
        if _try_lock(fh):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


def _try_lock(fh) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, ImportError):
        # ImportError：极少见的裁剪版 Python 两个模块都没有 → 退化成无锁。
        # 无锁比崩掉强：并发重复是可修复的，进程起不来不是。
        return False


def _release(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (OSError, ImportError):
        pass


def tune(conn) -> None:
    """给 SQLite 连接开 WAL + busy_timeout。

    WAL 让读写不互斥（检索不会被一次写入卡住），busy_timeout 让并发写自动重试而不是
    立刻抛 'database is locked'——那个异常正是多端同时用时最常见的崩溃原因。
    失败就算了：某些文件系统（网络盘）不支持 WAL，此时普通模式仍能工作。
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:  # noqa: BLE001
        pass
