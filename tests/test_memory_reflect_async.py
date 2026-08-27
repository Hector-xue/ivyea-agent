"""异步反思：节流、互斥、以及"绝不阻塞这一轮"。

改造前只有 CLI 退出路径会反思，而且是同步的 —— serve（用户主力入口）永远不反思。
改成每轮末尾问一句"够不够本"、够就后台跑之后，多出三个必须守住的性质：
反思不能扎堆、不能两个进程同时跑、失败不能变成每轮重试。
"""
from __future__ import annotations

import time

import pytest


def _episodes(n: int) -> None:
    from ivyea_agent import memory
    for i in range(n):
        memory.index_turn("user", f"第 {i} 条经历：帮我否个词", "s")


class _FakeProvider:
    """按脚本返回的假模型。calls 记下被调了几次。"""

    def __init__(self, payload: str = '{"operations": []}', boom: bool = False):
        self.payload, self.boom, self.calls = payload, boom, 0

    def complete(self, *a, **k):
        self.calls += 1
        if self.boom:
            raise RuntimeError("额度用尽")
        return self.payload


def test_threshold_lowered_to_eight(ivyea_home):
    from ivyea_agent import memory_reflect
    assert memory_reflect.MIN_EPISODES == 8


def test_should_reflect_needs_enough_episodes(ivyea_home):
    from ivyea_agent import memory_reflect
    _episodes(3)
    assert memory_reflect.should_reflect() is False
    _episodes(10)
    assert memory_reflect.should_reflect() is True


def test_throttle_blocks_back_to_back_runs(ivyea_home):
    """serve 是长驻的：一段密集对话几分钟内能反复越过显著性门槛，得有节流。"""
    from ivyea_agent import config, memory_reflect
    _episodes(20)
    assert memory_reflect.should_reflect() is True
    config.set_setting("memory_last_reflect_run_ts", time.time())
    assert memory_reflect.should_reflect() is False
    # 节流窗口过去之后又该跑了
    config.set_setting("memory_last_reflect_run_ts", time.time() - 10_000)
    assert memory_reflect.should_reflect() is True


def test_run_watermark_is_separate_from_episode_watermark(ivyea_home):
    """节流必须用**墙钟**水位线。

    经历水位线取的是最后一条经历的 ts；喂一批陈年经历会把它停在很久以前，
    拿它做节流等于节流永远不生效。
    """
    from ivyea_agent import memory_reflect
    assert memory_reflect._LAST_TS_KEY != memory_reflect._LAST_RUN_KEY


def test_failed_reflection_still_advances_throttle(ivyea_home):
    """模型欠费时经历只会越攒越多、门槛永远满足 —— 不推进水位线就是每轮重试一次 LLM。"""
    from ivyea_agent import memory_reflect
    _episodes(20)
    res = memory_reflect.reflect(_FakeProvider(boom=True))
    assert res["ok"] is False
    assert memory_reflect.last_run_ts() > 0
    assert memory_reflect.should_reflect() is False


def test_async_reflection_runs_in_background(ivyea_home, monkeypatch):
    from ivyea_agent import memory_reflect
    prov = _FakeProvider()
    monkeypatch.setattr(memory_reflect, "_default_provider", lambda: prov)
    _episodes(20)
    assert memory_reflect.maybe_reflect_async() is True
    assert memory_reflect.wait_for_idle(10.0) is True
    assert prov.calls == 1
    assert memory_reflect.is_running() is False


def test_async_reflection_skips_when_not_due(ivyea_home, monkeypatch):
    from ivyea_agent import memory_reflect
    prov = _FakeProvider()
    monkeypatch.setattr(memory_reflect, "_default_provider", lambda: prov)
    _episodes(2)                       # 不够门槛
    assert memory_reflect.maybe_reflect_async() is False
    assert prov.calls == 0


def test_async_reflection_survives_missing_provider(ivyea_home, monkeypatch):
    """没配 key 的机器上，轮末这一下必须无声跳过，不能让这一轮报错。"""
    from ivyea_agent import memory_reflect
    monkeypatch.setattr(memory_reflect, "_default_provider", lambda: None)
    _episodes(20)
    memory_reflect.maybe_reflect_async()
    assert memory_reflect.wait_for_idle(10.0) is True


def test_reflect_lock_is_not_the_write_lock(ivyea_home):
    """反思里包着一次最长 120 秒的模型调用。

    如果它占的是记忆写锁，这两分钟内所有 memory_write / core_memory_edit 都要排队，
    用户会看到"说了记住、半天没反应"。两把锁必须是两个文件。
    """
    from ivyea_agent import memory_lock
    assert memory_lock.lock_path() != memory_lock.reflect_lock_path()


def test_reflect_lock_is_non_blocking_by_default(ivyea_home):
    """拿不到就走人：反思是周期性的，排队等只会让线程堆积。"""
    from ivyea_agent import memory_lock
    with memory_lock.reflect_lock(timeout=0.0) as first:
        assert first is True
        t0 = time.time()
        with memory_lock.reflect_lock(timeout=0.0) as second:
            # 同进程内 flock 可重入，拿到与否都行；关键是**不许阻塞**
            assert second in (True, False)
        assert time.time() - t0 < 1.0
