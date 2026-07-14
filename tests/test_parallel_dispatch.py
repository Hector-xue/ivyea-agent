"""Parallel dispatch of read-only tool calls (order preserved, concurrency real)."""
from __future__ import annotations

import time

from ivyea_agent import agent_loop
from ivyea_agent.agent_tools import ToolContext, ToolResult


def _silent(_s):
    pass


def test_parallel_readonly_preserves_order_and_runs_concurrently(monkeypatch):
    sleep_s, n = 0.5, 4

    def fake(name, args, ctx):
        time.sleep(sleep_s)
        return ToolResult(True, f"got-{args['k']}")

    monkeypatch.setattr(agent_loop, "dispatch_result", fake)
    ctx, messages = ToolContext(), []
    status = agent_loop.TurnStatus(max_steps=10)
    calls = [{"id": f"c{i}", "name": "read_file", "arguments": {"k": i}} for i in range(n)]

    t0 = time.time()
    agent_loop._dispatch_tool_calls(ctx, messages, status, calls, 0, 10, _silent)
    elapsed = time.time() - t0

    assert [m["tool_call_id"] for m in messages] == [f"c{i}" for i in range(n)]
    assert [m["content"] for m in messages] == [f"got-{i}" for i in range(n)]
    assert status.tool_calls == n
    # Sequential would take n*sleep_s; require finishing faster than (n-1) sleeps
    # to prove real overlap. This keeps a large absolute margin for thread-startup
    # / GIL overhead on slow, contended CI runners — a tighter 0.6*n*sleep bound
    # with sleep_s=0.3 was flaky on windows-latest · py3.11 (0.86s vs 0.72s).
    assert elapsed < sleep_s * (n - 1), f"not concurrent: {elapsed:.2f}s"


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
