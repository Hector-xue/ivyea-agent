"""一轮的预算：记账调用退款、成本闸、停下来的理由要说清楚。"""
from __future__ import annotations

import threading

from ivyea_agent import agent_loop, budget
from ivyea_agent.agent_tools import ToolContext, ToolResult


def test_bookkeeping_calls_do_not_eat_the_quota():
    """实测：300 次工具调用里 112 次是纯记账。它们不推进任务，就不该占配额。"""
    b = budget.TurnBudget(max_steps=5)
    for _ in range(20):
        b.consume("progress_update")
        b.consume("todo_write")
    assert b.steps_used == 0
    assert b.steps_refunded == 40
    assert b.steps_exhausted() is False


def test_real_work_does_eat_the_quota():
    b = budget.TurnBudget(max_steps=3)
    for _ in range(3):
        b.consume("read_file")
    assert b.steps_used == 3
    assert b.steps_exhausted() is True
    assert b.stop_reason() == "steps"


def test_cost_gate_is_off_unless_configured():
    b = budget.TurnBudget(max_steps=100)
    b.add_cost(999.0)
    assert b.cost_exhausted() is False
    assert b.stop_reason() == ""


def test_cost_gate_trips_when_configured():
    b = budget.TurnBudget(max_steps=100, max_cost_cny=1.0)
    b.add_cost(0.6)
    assert b.exhausted() is False
    b.add_cost(0.5)
    assert b.cost_exhausted() is True
    assert b.stop_reason() == "cost"      # 钱的优先级高于步数：要采取的动作不一样


def test_budget_is_thread_safe():
    b = budget.TurnBudget(max_steps=10 ** 6)

    def work():
        for _ in range(500):
            b.consume("read_file")
            b.add_cost(0.001)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.steps_used == 4000
    assert round(b.cost_cny, 3) == 4.0


def test_from_settings_respects_an_explicit_cap(ivyea_home):
    from ivyea_agent import config
    config.set_setting("chat_max_tool_steps", 42)
    config.set_setting("chat_max_cost_cny", 2.5)
    assert budget.from_settings().max_steps == 42
    assert budget.from_settings().max_cost_cny == 2.5
    assert budget.from_settings(7).max_steps == 7      # 调用方显式传的说了算


def test_a_broken_setting_falls_back(ivyea_home):
    from ivyea_agent import config
    config.set_setting("chat_max_cost_cny", "不是数字")
    assert budget.from_settings().max_cost_cny == 0.0


# ── 接线 ─────────────────────────────────────────────────────────────────────
def test_the_turn_status_refunds_bookkeeping():
    b = budget.TurnBudget(max_steps=10)
    status = agent_loop.TurnStatus(max_steps=10, budget=b)
    status.record_tool_call("todo_write")
    status.record_tool_call("read_file")
    assert status.tool_calls == 2          # 时间线序号要连续，两次都算
    assert b.steps_used == 1               # 但只有一次占配额


def test_the_limit_text_says_which_gate_was_hit():
    steps = budget.TurnBudget(max_steps=2)
    steps.consume("read_file"), steps.consume("read_file")
    assert "步" in agent_loop._limit_text(2, steps)

    cost = budget.TurnBudget(max_steps=100, max_cost_cny=1.0)
    cost.add_cost(1.5)
    text = agent_loop._limit_text(100, cost)
    assert "成本上限" in text
    assert "chat_max_cost_cny" in text     # 告诉用户怎么放宽


def test_the_payload_reports_the_refund(ivyea_home):
    b = budget.TurnBudget(max_steps=2)
    status = agent_loop.TurnStatus(max_steps=2, budget=b)
    for name in ("todo_write", "todo_write", "read_file", "read_file"):
        status.record_tool_call(name)
    ctx = ToolContext(workspace=".", session_id="b1")
    text = agent_loop._limit_payload(2, status, ctx)
    assert "记账调用未计入" in text


def test_a_bookkeeping_storm_still_hits_the_hard_ceiling():
    """预算退款是为了让真活有配额，不是给死循环发无限票 —— 天花板是最后一道保险。"""
    assert agent_loop._hard_step_ceiling(10) == 30
    assert agent_loop._hard_step_ceiling(0) >= 1


def test_step_cost_never_raises():
    class Weird:
        model = None
    assert agent_loop._step_cost(Weird(), {"prompt_tokens": 10}) >= 0.0
    assert agent_loop._step_cost(None, None) == 0.0


def test_hitting_the_cost_gate_marks_the_plan_for_resume(ivyea_home):
    """撞闸停下来时，"停在哪"必须落盘 —— 否则命令行这条路进程一退就只剩一句提示。"""
    from ivyea_agent import plan_store
    plan_store.sync_todos("b2", [{"content": "第一步", "status": "in_progress"},
                                 {"content": "第二步", "status": "pending"}])
    b = budget.TurnBudget(max_steps=100, max_cost_cny=0.5)
    b.add_cost(1.0)
    status = agent_loop.TurnStatus(max_steps=100, budget=b)
    ctx = ToolContext(workspace=".", session_id="b2")
    agent_loop._finalize_limit(ctx, [], status, 100)
    assert "未收尾" in plan_store.replan_reason("b2")


def test_observe_still_works_without_a_budget():
    """budget=None 是老行为，必须原样可用（serve 里的子 agent 走这条）。"""
    status = agent_loop.TurnStatus(max_steps=5)
    status.record_tool_call("read_file")
    status.observe_tool_result("read_file", ToolResult(True, "x"), {"path": "a.py"})
    assert status.tool_calls == 1


# ── 天花板出口（复核时打出来的假话） ─────────────────────────────────────────
def test_hitting_the_ceiling_is_a_distinct_stop_reason():
    b = budget.TurnBudget(max_steps=5)
    for _ in range(15):
        b.consume("todo_write")          # 全是记账 → 预算一步没用
    assert b.stop_reason() == ""         # 还没标记之前不算停
    b.mark_ceiling()
    assert b.stop_reason() == "ceiling"
    assert b.steps_used == 0


def test_the_ceiling_message_does_not_lie_or_misadvise():
    """此前这里说"已达安全上限 5 步"（假：预算一步没用），
    还建议把 chat_max_tool_steps 调到 10（无效：预算根本不是瓶颈）。"""
    b = budget.TurnBudget(max_steps=5)
    for _ in range(15):
        b.consume("todo_write")
    b.mark_ceiling()
    text = agent_loop._limit_text(5, b)
    assert "记账调用" in text
    assert "不会有帮助" in text
    assert "chat_max_tool_steps 10" not in text     # 别再给这条没用的建议


def test_a_real_step_exhaustion_still_advises_raising_the_cap():
    b = budget.TurnBudget(max_steps=3)
    for _ in range(3):
        b.consume("read_file")
    assert b.stop_reason() == "steps"
    assert "chat_max_tool_steps" in agent_loop._limit_text(3, b)


def test_cost_outranks_ceiling():
    b = budget.TurnBudget(max_steps=100, max_cost_cny=1.0)
    b.add_cost(2.0)
    b.mark_ceiling()
    assert b.stop_reason() == "cost"     # 钱的优先级最高：要用户拍板的是它
