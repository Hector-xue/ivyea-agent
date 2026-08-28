"""打转守卫：重复调用拦截、空转重规划、豁免名单、并发安全。"""
from __future__ import annotations

import threading

from ivyea_agent import agent_loop, loop_guard, plan_store
from ivyea_agent.agent_tools import ToolContext


def _drive(guard, name, args, *, ok=False, text="同一份报错", times=1):
    """模拟 times 次「检查 → 执行 → 观察」。返回最后一次的拦截文案（None=放行）。"""
    blocked = None
    for _ in range(times):
        blocked = guard.check(name, args)
        if blocked:
            continue
        guard.observe(name, args, ok, text)
    return blocked


def test_identical_call_is_blocked_after_the_limit():
    guard = loop_guard.LoopGuard(repeat_limit=3, stall_limit=99)
    args = {"pattern": "找不到的东西"}
    assert _drive(guard, "grep", args, times=3) is None      # 前 3 次照常放行
    blocked = guard.check("grep", args)
    assert blocked is not None
    assert "重复调用" in blocked and "3 次" in blocked


def test_second_block_is_terser_and_still_blocks():
    guard = loop_guard.LoopGuard(repeat_limit=2, stall_limit=99)
    args = {"command": "false"}
    _drive(guard, "run_command", args, times=2)
    first = guard.check("run_command", args)
    second = guard.check("run_command", args)
    assert first and second
    assert second != first
    assert "重复调用" in second or "重复调用" in first


def test_different_arguments_are_not_repeats():
    guard = loop_guard.LoopGuard(repeat_limit=2, stall_limit=99)
    for i in range(6):
        assert guard.check("read_file", {"path": f"a{i}.py"}) is None
        guard.observe("read_file", {"path": f"a{i}.py"}, True, f"内容 {i}")


def test_polling_and_bookkeeping_tools_are_exempt():
    """bash_output 就是要拿同一个 id 反复问；todo_write 本来就会重复出现。"""
    guard = loop_guard.LoopGuard(repeat_limit=2, stall_limit=99)
    for _ in range(10):
        assert guard.check("bash_output", {"bash_id": "b1"}) is None
        guard.observe("bash_output", {"bash_id": "b1"}, True, "（无新输出）")
        assert guard.check("todo_write", {"todos": []}) is None
        guard.observe("todo_write", {"todos": []}, True, "已更新计划：0/0 完成。")


def test_repeated_identical_results_count_as_no_progress():
    # 第一次撞到"扫描 0 文件"是**新信息**（这条路走不通也是情报），所以要跑 stall_limit+1 次
    # 才攒够 stall_limit 步空转。
    guard = loop_guard.LoopGuard(repeat_limit=99, stall_limit=4)
    for i in range(5):
        guard.observe("grep", {"pattern": f"p{i}"}, True, "⚠ 扫描 0 文件")
    feedback = guard.stall_feedback()
    assert feedback is not None
    assert "没有产生任何新证据" in feedback
    assert "todo_write 修订计划" in feedback


def test_new_evidence_resets_the_stall_counter():
    guard = loop_guard.LoopGuard(repeat_limit=99, stall_limit=3)
    for i in range(2):
        guard.observe("grep", {"pattern": f"p{i}"}, True, "⚠ 扫描 0 文件")
    guard.observe("read_file", {"path": "a.py"}, True, "def main(): ...")
    assert guard.steps_since_progress == 0
    assert guard.stall_feedback() is None


def test_meta_tools_neither_advance_nor_stall():
    guard = loop_guard.LoopGuard(repeat_limit=99, stall_limit=3)
    guard.observe("grep", {"pattern": "x"}, True, "⚠ 扫描 0 文件")
    before = guard.steps_since_progress
    for _ in range(5):
        guard.observe("progress_update", {"kind": "phase_end"}, True, "已记录")
    assert guard.steps_since_progress == before


def test_guard_is_thread_safe_under_parallel_dispatch():
    guard = loop_guard.LoopGuard(repeat_limit=1000, stall_limit=10 ** 6)
    def work():
        for _ in range(200):
            guard.observe("grep", {"pattern": "x"}, True, "同样的结果")
    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert guard.steps_since_progress == 8 * 200 - 1     # 只有第一次算进展


# ── 与 agent_loop 的接线 ─────────────────────────────────────────────────────
def test_agent_loop_blocks_a_repeating_tool_call(ivyea_home):
    ctx = ToolContext(workspace=".", session_id="loop-1")
    guard = loop_guard.LoopGuard(repeat_limit=2, stall_limit=99)
    tc = {"id": "t1", "name": "grep", "arguments": {"pattern": "zzz"}}
    for _ in range(2):
        guard.observe("grep", tc["arguments"], True, "⚠ 扫描 0 文件")
    res, _ms, blocked = agent_loop._run_one(tc, ctx, guard)
    assert blocked is True and res.ok is False
    assert "重复调用" in res.text


def test_agent_loop_stall_marks_the_plan_for_replanning(ivyea_home):
    ctx = ToolContext(workspace=".", session_id="loop-2")
    plan_store.sync_todos("loop-2", [{"content": "查根因", "status": "in_progress"}])
    guard = loop_guard.LoopGuard(repeat_limit=99, stall_limit=3)
    for i in range(4):
        guard.observe("grep", {"pattern": f"p{i}"}, True, "⚠ 扫描 0 文件")
    res, _ms, blocked = agent_loop._run_one(
        {"id": "t2", "name": "grep", "arguments": {"pattern": "又一次"}}, ctx, guard)
    assert blocked is True and res.ok is False
    assert "没有产生任何新证据" in res.text
    assert "原地打转" in plan_store.replan_reason("loop-2")


def test_disabled_thresholds_never_block(ivyea_home, monkeypatch):
    """把阈值设成 0 = 关掉这道守卫，行为回到加它之前。"""
    from ivyea_agent import config
    monkeypatch.setattr(config, "get_setting",
                        lambda key, default=None: 0 if key.startswith("loop_guard_") else default)
    guard = agent_loop._new_loop_guard()
    for _ in range(50):
        assert guard.check("grep", {"pattern": "x"}) is None
        guard.observe("grep", {"pattern": "x"}, True, "同样的结果")
    assert guard.stall_feedback() is None
