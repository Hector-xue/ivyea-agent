"""Local action queue."""
from __future__ import annotations

from ivyea_agent.actions import Action


def test_enqueue_list_status_clear(ivyea_home):
    from ivyea_agent import action_queue

    acts = [Action(kind="negative", search_term="bad term", asin="B0X")]
    added = action_queue.enqueue_actions(acts, source="run1", origin="test")
    assert len(added) == 1
    assert action_queue.list_items()[0]["summary"].startswith("否词")

    # duplicate pending action is skipped
    assert action_queue.enqueue_actions(acts, source="run1", origin="test") == []

    item_id = added[0]["id"]
    assert action_queue.set_status(item_id, "approved") is True
    assert action_queue.get(item_id)["status"] == "approved"
    assert action_queue.clear("approved") == 1
    assert action_queue.list_items() == []


def test_blocked_action_kept_with_reason(ivyea_home):
    from ivyea_agent import action_queue

    a = Action(kind="negative", search_term="brand term", blocked=True, block_reason="品牌词不否")
    added = action_queue.enqueue_actions([a], source="run2")
    out = action_queue.render(added)
    assert "BLOCKED" in out and "品牌词不否" in out


def test_to_action_and_mark_done(ivyea_home):
    from ivyea_agent import action_queue

    added = action_queue.enqueue_actions(
        [Action(kind="negative", search_term="waste term", asin="B0Y")],
        source="run3",
        origin="test",
    )
    item = added[0]
    action = action_queue.to_action(item)
    assert action.kind == "negative"
    assert action.search_term == "waste term"
    assert action.asin == "B0Y"

    assert action_queue.mark_done(item["id"], "executed ok") is True
    saved = action_queue.get(item["id"])
    assert saved["status"] == "done"
    assert saved["result"] == "executed ok"


def test_cli_execute_dry_run_keeps_item_approved(ivyea_home, capsys):
    from ivyea_agent import action_queue
    from ivyea_agent.cli import build_parser

    added = action_queue.enqueue_actions(
        [Action(kind="negative", search_term="waste term", asin="B0Y")],
        source="run4",
        origin="test",
    )
    item_id = added[0]["id"]
    assert action_queue.set_status(item_id, "approved") is True

    parser = build_parser()
    args = parser.parse_args(["action", "execute", item_id])
    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert item_id in out
    assert action_queue.get(item_id)["status"] == "approved"


def test_bulk_status_and_report(ivyea_home):
    from ivyea_agent import action_queue

    action_queue.enqueue_actions([
        Action(kind="negative", search_term="term one"),
        Action(kind="negative", search_term="term two"),
    ], source="run5", origin="test")

    assert action_queue.set_many_status("approved", from_status="pending", limit=10) == 2
    assert action_queue.summary()["approved"] == 2

    items = action_queue.list_items(status="approved")
    report = action_queue.render_report(items)
    assert "# Ivyea 动作队列复核报告" in report
    assert "term one" in report or "term two" in report


def test_cli_bulk_approve_and_report(ivyea_home, tmp_path, capsys):
    from ivyea_agent import action_queue
    from ivyea_agent.cli import build_parser

    action_queue.enqueue_actions([Action(kind="negative", search_term="term one")], source="run6", origin="test")
    parser = build_parser()
    args = parser.parse_args(["action", "approve", "--all"])
    assert args.func(args) == 0
    assert action_queue.summary()["approved"] == 1

    out_path = tmp_path / "queue.md"
    args = parser.parse_args(["action", "report", "--status", "approved", "--output", str(out_path)])
    assert args.func(args) == 0
    assert out_path.exists()
    assert "term one" in out_path.read_text(encoding="utf-8")
    assert "已导出报告" in capsys.readouterr().out
