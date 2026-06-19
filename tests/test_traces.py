from __future__ import annotations


def test_trace_record_stats_and_render(ivyea_home):
    from ivyea_agent import traces

    traces.record("s1", "t1", "tool_call", "knowledge_search", ok=True, duration_ms=12, summary="ok")
    traces.record("s1", "t1", "tool_call", "run_patrol", ok=False, duration_ms=30, summary="failed")

    st = traces.stats()
    assert st["tool_calls"] == 2
    assert st["failures"] == 1
    assert st["avg_tool_ms"] == 21

    text = traces.render_recent(limit=5, session_id="s1")
    assert "knowledge_search" in text
    assert "run_patrol" in text


def test_trace_cli(ivyea_home, capsys):
    from ivyea_agent import traces
    from ivyea_agent.cli import main

    traces.record("s2", "t1", "tool_call", "skill_search", summary="ok")
    assert main(["trace", "stats"]) == 0
    out = capsys.readouterr().out
    assert "tool_calls 1" in out

    assert main(["trace", "--session", "s2"]) == 0
    out = capsys.readouterr().out
    assert "skill_search" in out
