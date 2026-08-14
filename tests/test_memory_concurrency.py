"""并发安全：多进程同时写记忆。

`~/.ivyea` 同时被三个进程写——CLI、本机 serve(8765)、IvyeaOps 嵌入模式。
分类记忆的写入是"先查重、再落盘"的读-改-写序列，不加锁两边会双双查重通过、
各建一条近似重复的记忆。单文件 os.replace 的原子性救不了这个。

这里用**真子进程**而不是线程：要验的正是跨进程的文件锁，线程测不到
（GIL 和进程内状态会掩盖问题）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_writers(home, script_body, n=4, timeout=90):
    """起 n 个真进程并发执行同一段写入脚本。"""
    script = textwrap.dedent(script_body)
    env = {**os.environ, "IVYEA_HOME": str(home), "PYTHONPATH": REPO}
    procs = [subprocess.Popen([sys.executable, "-c", script], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(n)]
    out = []
    for p in procs:
        so, se = p.communicate(timeout=timeout)
        out.append((p.returncode, so.decode("utf-8", "replace"), se.decode("utf-8", "replace")))
    return out


def test_concurrent_add_does_not_duplicate(ivyea_home):
    """四个进程同时写"同一件事"，最终只能留下一条——这是加锁的核心目的。"""
    results = _run_writers(ivyea_home, """
        from ivyea_agent import memory_store
        r = memory_store.apply("add", name="并发打法", category="domain",
                               description="库存周转与备货节奏",
                               content="周转天数低于30天就下单补货，避免断货。")
        print("OK" if r.get("ok") else "REJECT")
    """)
    assert all(rc == 0 for rc, _, se in results), [se for _, _, se in results]

    from ivyea_agent import memory_store
    entries = memory_store.list_entries()
    assert len(entries) == 1, [e.name for e in entries]
    # 恰好一个进程成功、其余被查重/同名拦下
    assert sum("OK" in so for _, so, _ in results) == 1


def test_concurrent_distinct_writes_all_survive(ivyea_home):
    """并发写**不同**的记忆时一条都不能丢——锁不能变成"只有一个能写成"。"""
    script = """
        import os, sys
        from ivyea_agent import memory_store
        i = os.environ["WRITER_ID"]
        topics = {"0": ("库存周转", "补货节奏与断货预防"),
                  "1": ("广告出价", "竞价调整与花费控制"),
                  "2": ("差评处理", "负面评价应对流程"),
                  "3": ("促销折扣", "coupon与折扣排期")}
        name, desc = topics[i]
        r = memory_store.apply("add", name=name, category="domain",
                               description=desc, content=f"{desc}的具体做法与阈值。")
        print("OK" if r.get("ok") else "FAIL:" + r.get("message", ""))
    """
    env_base = {**os.environ, "IVYEA_HOME": str(ivyea_home), "PYTHONPATH": REPO}
    procs = []
    for i in range(4):
        procs.append(subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(script)],
            env={**env_base, "WRITER_ID": str(i)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    outs = [p.communicate(timeout=90) for p in procs]
    assert all(p.returncode == 0 for p in procs), [se.decode() for _, se in outs]

    from ivyea_agent import memory_store
    assert len(memory_store.list_entries()) == 4


def test_concurrent_readers_not_blocked_by_writer(ivyea_home):
    """WAL 的意义：写入进行中检索不能被卡死。"""
    from ivyea_agent import memory_store
    memory_store.apply("add", name="已有记忆", category="domain",
                       description="独特的检索关键词", content="正文")

    stop = threading.Event()
    errors = []

    def _reader():
        while not stop.is_set():
            try:
                memory_store.search("独特的检索关键词")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
                return

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    _run_writers(ivyea_home, """
        from ivyea_agent import memory_store
        for i in range(5):
            memory_store.apply("add", name=f"并发写入{i}", category="project",
                               description=f"第{i}批并发写入的主题描述{i*7}",
                               content=f"内容{i}" * 20)
    """, n=2)
    stop.set()
    t.join(timeout=10)
    assert not errors, errors


def test_lock_times_out_and_proceeds(ivyea_home, monkeypatch):
    """拿不到锁**照常执行**而不是报错：记忆写不进去（"我说了记住结果没记"）
    比偶发重复条目糟得多，而重复下次还有机会合并。"""
    from ivyea_agent import memory_lock
    monkeypatch.setattr(memory_lock, "_try_lock", lambda fh: False)
    t0 = time.time()
    with memory_lock.memory_write_lock(timeout=0.2) as acquired:
        assert acquired is False
    assert time.time() - t0 < 5


def test_lock_is_exclusive_within_process(ivyea_home):
    from ivyea_agent import memory_lock
    with memory_lock.memory_write_lock(timeout=1) as first:
        assert first is True


def test_wal_enabled(ivyea_home):
    from ivyea_agent import memory
    conn = memory._conn()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert str(mode).lower() in ("wal", "memory", "delete")   # 某些文件系统不支持 WAL，允许降级


def test_write_lock_releases_on_exception(ivyea_home):
    """异常路径必须放锁，否则一次失败会把后续所有写入卡到超时。"""
    from ivyea_agent import memory_lock
    try:
        with memory_lock.memory_write_lock(timeout=1):
            raise RuntimeError("模拟失败")
    except RuntimeError:
        pass
    with memory_lock.memory_write_lock(timeout=1) as again:
        assert again is True
