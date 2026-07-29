"""Parallel dispatch of read-only tool calls (order preserved, concurrency real)."""
from __future__ import annotations

import threading
import time

from ivyea_agent import agent_loop
from ivyea_agent.agent_tools import ToolContext, ToolResult


def _silent(_s):
    pass


def test_parallel_readonly_preserves_order_and_runs_concurrently(monkeypatch):
    n = 4
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}

    def fake(name, args, ctx):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.3)
        with lock:
            live["now"] -= 1
        return ToolResult(True, f"got-{args['k']}")

    monkeypatch.setattr(agent_loop, "dispatch_result", fake)
    ctx, messages = ToolContext(), []
    status = agent_loop.TurnStatus(max_steps=10)
    calls = [{"id": f"c{i}", "name": "read_file", "arguments": {"k": i}} for i in range(n)]

    agent_loop._dispatch_tool_calls(ctx, messages, status, calls, 0, 10, _silent)

    assert [m["tool_call_id"] for m in messages] == [f"c{i}" for i in range(n)]
    assert [m["content"] for m in messages] == [f"got-{i}" for i in range(n)]
    assert status.tool_calls == n
    # Overlap measured directly (peak simultaneous calls) instead of wall clock:
    # a contended CI runner makes any elapsed-time bound flaky — the previous
    # `elapsed < sleep*(n-1)` still failed on windows-latest (1.84s vs 1.5s)
    # even though the calls did overlap.
    assert live["peak"] > 1, f"not concurrent: peak={live['peak']}"


def test_mixed_batch_runs_sequentially(monkeypatch):
    order = []

    def fake(name, args, ctx):
        order.append(name)
        return ToolResult(True, "ok")

    monkeypatch.setattr(agent_loop, "dispatch_result", fake)
    ctx, messages = ToolContext(), []
    status = agent_loop.TurnStatus(max_steps=10)
    # write_file is not parallel-safe -> the whole batch must stay sequential & ordered
    calls = [{"id": "a", "name": "read_file", "arguments": {}},
             {"id": "b", "name": "write_file", "arguments": {}}]
    agent_loop._dispatch_tool_calls(ctx, messages, status, calls, 0, 10, _silent)
    assert order == ["read_file", "write_file"]
    assert [m["tool_call_id"] for m in messages] == ["a", "b"]
