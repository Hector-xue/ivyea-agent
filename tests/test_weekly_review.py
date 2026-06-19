from __future__ import annotations

from ivyea_agent.actions import Action


def test_weekly_review_build_and_render(ivyea_home):
    from ivyea_agent import action_queue, memory, weekly_review

    added = action_queue.enqueue_actions([
        Action(kind="negative", search_term="bad term"),
        Action(kind="reduce_bid", search_term="expensive term", blocked=True, block_reason="保护词"),
    ], source="weekly", origin="test")
    action_queue.set_status(added[0]["id"], "approved")
    memory.record_run("B0X", negatives=1, scale=0, reduce=1)

    report = weekly_review.build(limit=50)
    assert report["queue"]["approved"] == 1
    text = weekly_review.render(report)
    assert "Ivyea 周期运营复盘" in text
    assert "本周期优先事项" in text
    assert "最近巡检" in text


def test_weekly_cli_export(ivyea_home, tmp_path, capsys):
    from ivyea_agent.cli import main

    out = tmp_path / "weekly.md"
    assert main(["weekly", "review", "--output", str(out)]) == 0
    assert out.exists()
    assert "周期运营复盘" in out.read_text(encoding="utf-8")
    assert "已导出周期复盘" in capsys.readouterr().out
