"""打转守卫：重复调用拦截、空转重规划、豁免名单、并发安全。"""
from __future__ import annotations

import threading

from ivyea_agent import agent_loop, loop_guard, plan_store
from ivyea_agent.agent_tools import ToolContext


def _drive(guard, name, args, *, ok=True, text="一份普通结果", times=1):
    """模拟 times 次「检查 → 执行 → 观察」。返回最后一次的拦截文案（None=放行）。"""
    blocked = None
    for _ in range(times):
        blocked = guard.check(name, args)
        if blocked:
            continue
        guard.observe(name, args, ok, text)
    return blocked


def test_identical_call_is_blocked_after_the_limit():
    # 结果**不是**拒绝：这里验的是参数指纹那条路（拒绝连击是另一条，见下面的用例）。
    guard = loop_guard.LoopGuard(repeat_limit=3, stall_limit=99)
    args = {"pattern": "找不到的东西"}
    assert _drive(guard, "grep", args, times=3) is None      # 前 3 次照常放行
    blocked = guard.check("grep", args)
    assert blocked is not None
    assert "重复调用" in blocked and "3 次" in blocked


def test_second_block_is_terser_and_still_blocks():
    guard = loop_guard.LoopGuard(repeat_limit=2, stall_limit=99)
    args = {"command": "false"}
    _drive(guard, "run_command", args, times=2, text="一份普通结果")
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
        guard.observe("grep", tc["arguments"], True, f"命中 {_} 处")   # 非拒绝结果
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


# ── 拒绝连击（实测打出来的那个洞） ──────────────────────────────────────────
#
# 实测：模型连发 90 次 progress_update(kind='phase_end')，每次把 summary 的措辞改一点，
# 于是**参数指纹永远对不上**，而拿回来的拒绝一字不差。真正说明"卡住了"的是结果不是参数。
def test_same_rejection_over_and_over_is_blocked_even_with_shifting_args():
    guard = loop_guard.LoopGuard(repeat_limit=3, stall_limit=99)
    reject = "⚠ phase_end 必须结束当前正在汇报的阶段。"
    for i in range(3):
        assert guard.check("progress_update", {"kind": "phase_end", "summary": f"第 2 步完成 {i}"}) is None
        guard.observe("progress_update", {"kind": "phase_end", "summary": f"第 2 步完成 {i}"},
                      True, reject)
    blocked = guard.check("progress_update", {"kind": "phase_end", "summary": "换个说法再来一次"})
    assert blocked is not None
    assert "完全相同的拒绝" in blocked


def test_a_success_in_between_clears_the_rejection_streak():
    guard = loop_guard.LoopGuard(repeat_limit=2, stall_limit=99)
    guard.observe("progress_update", {"kind": "phase_end"}, True, "⚠ 不行")
    guard.observe("progress_update", {"kind": "phase_end"}, True, "⚠ 不行")
    guard.observe("progress_update", {"kind": "phase_start"}, True, "阶段 2 开始")
    assert guard.check("progress_update", {"kind": "phase_end"}) is None


def test_a_different_rejection_restarts_the_streak():
    """换了一句拒绝说明模型确实换了做法，不该被当成打转。"""
    guard = loop_guard.LoopGuard(repeat_limit=2, stall_limit=99)
    guard.observe("todo_write", {"todos": []}, True, "⚠ Todo 更新已拒绝：A")
    guard.observe("todo_write", {"todos": []}, True, "⚠ Todo 更新已拒绝：B")
    assert guard.check("todo_write", {"todos": []}) is None


def test_polling_never_trips_the_rejection_streak():
    guard = loop_guard.LoopGuard(repeat_limit=2, stall_limit=99)
    for _ in range(10):
        guard.observe("bash_output", {"bash_id": "b1"}, True, "⚠ 该后台任务不存在")
        assert guard.check("bash_output", {"bash_id": "b1"}) is None


def test_rotating_rejections_are_blocked_too():
    """实测第二形态：模型在三四句**不同**的拒绝之间轮着撞，一句都不连续重复。"""
    guard = loop_guard.LoopGuard(repeat_limit=3, stall_limit=99)
    rejects = ["⚠ 必须提供 evidence。", "⚠ 还没有真实工具结果。", "⚠ 当前阶段尚未 phase_end。"]
    for i in range(6):
        assert guard.check("progress_update", {"n": i}) is None
        guard.observe("progress_update", {"n": i}, True, rejects[i % 3])
    blocked = guard.check("progress_update", {"n": 99})
    assert blocked is not None
    assert "连续 6 次被拒绝" in blocked


def test_one_success_clears_the_rotating_streak():
    guard = loop_guard.LoopGuard(repeat_limit=2, stall_limit=99)
    for i in range(4):
        guard.observe("progress_update", {"n": i}, True, f"⚠ 拒绝 {i % 2}")
    guard.observe("progress_update", {"n": 9}, True, "阶段 2 开始")
    assert guard.check("progress_update", {"n": 10}) is None
