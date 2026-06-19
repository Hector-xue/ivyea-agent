"""Operational scorecard."""
from __future__ import annotations

from ivyea_agent.actions import Action


def test_scorecard_build_and_render(ivyea_home):
    from ivyea_agent import action_queue, memory, scorecard

    added = action_queue.enqueue_actions([
        Action(kind="negative", search_term="bad one"),
        Action(kind="negative", search_term="bad two"),
    ], source="run-score", origin="test")
    action_queue.set_status(added[0]["id"], "approved")
    action_queue.mark_done(added[1]["id"], "ok")
    memory.record_decision("B0X", "bad one", "negative", "approve")
    memory.record_run("B0X", negatives=2, scale=1, reduce=0)

    s = scorecard.build()
    assert s["queue"]["approved"] == 1
    assert s["queue"]["done"] == 1
    assert s["approval_rate"] == 1.0

    text = scorecard.render_md(s)
    assert "Ivyea Agent 运营 Scorecard" in text
    assert "建议采纳率：100%" in text
    assert "最近巡检" in text


def test_cli_scorecard_export(ivyea_home, tmp_path, capsys):
    from ivyea_agent.cli import build_parser

    out = tmp_path / "score.md"
    parser = build_parser()
    args = parser.parse_args(["scorecard", "--output", str(out)])
    assert args.func(args) == 0
    assert out.exists()
    assert "Scorecard" in out.read_text(encoding="utf-8")
    assert "已导出 Scorecard" in capsys.readouterr().out

