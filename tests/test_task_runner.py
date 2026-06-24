from __future__ import annotations

from argparse import Namespace

from ivyea_agent import task_runner


def test_task_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(task_runner, "TASK_DIR", tmp_path / "tasks")
    task = task_runner.create("Ship workspace agent", steps=["index repo", "run tests"], notes="local only")
    assert task["status"] == "pending"
    assert len(task["steps"]) == 2

    task = task_runner.start_next(task["id"])
    assert task["status"] == "in_progress"
    assert task["steps"][0]["status"] == "in_progress"

    task = task_runner.update_step(task["id"], 1, "completed", "done")
    assert task["status"] == "in_progress"
    assert task["steps"][0]["notes"] == "done"

    task = task_runner.start_next(task["id"])
    assert task["steps"][1]["status"] == "in_progress"
    resume = task_runner.render_resume(task)
    assert "#2" in resume and "run tests" in resume

    task = task_runner.update_step(task["id"], 2, "completed")
    assert task["status"] == "completed"
    assert task_runner.progress(task)["done"] == 2


def test_task_block_log_list_and_render(tmp_path, monkeypatch):
    monkeypatch.setattr(task_runner, "TASK_DIR", tmp_path / "tasks")
    task = task_runner.create("Fix CI", steps=["inspect logs", "patch"])
    task = task_runner.update_step(task["id"], 1, "blocked", "missing token")
    assert task["status"] == "blocked"
    task = task_runner.append_log(task["id"], "Need auth")
    assert task["events"][-1]["text"] == "Need auth"

    rows = task_runner.list_tasks()
    assert [r["id"] for r in rows] == [task["id"]]
    assert task_runner.list_tasks(status="completed") == []
    out = task_runner.render(task)
    assert "Fix CI" in out
    assert "missing token" in out


def test_set_status_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(task_runner, "TASK_DIR", tmp_path / "tasks")
    task = task_runner.create("Manual gate")
    task = task_runner.set_status(task["id"], "cancelled", "user stopped")
    assert task["status"] == "cancelled"
    try:
        task_runner.set_status(task["id"], "bad")
    except ValueError as e:
        assert "未知任务状态" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_record_interruption_blocks_active_step(tmp_path, monkeypatch):
    monkeypatch.setattr(task_runner, "TASK_DIR", tmp_path / "tasks")
    task = task_runner.create("Long run", steps=["inspect", "patch"])
    task = task_runner.start_next(task["id"])
    task = task_runner.record_interruption(
        task["id"],
        "tool_step_limit",
        "继续时不要重复",
        state={"session_id": "sid-1", "turn_id": "turn-1", "max_steps": 2, "tool_calls": 2},
    )
    assert task["status"] == "blocked"
    assert task["steps"][0]["status"] == "blocked"
    assert "不要重复" in task["steps"][0]["notes"]
    assert task["resume"]["reason"] == "tool_step_limit"
    assert task["resume"]["state"]["tool_calls"] == 2
    assert "上一轮工具调用：2/2" in task["resume"]["prompt"]
    assert "不要重复上一轮已经成功的工具调用" in task["resume"]["prompt"]
    assert task["events"][-1]["kind"] == "interrupted"


def test_resume_payload_for_plain_task(tmp_path, monkeypatch):
    monkeypatch.setattr(task_runner, "TASK_DIR", tmp_path / "tasks")
    task = task_runner.create("Continue work", steps=["read", "patch"])

    payload = task_runner.resume_payload(task["id"])
    assert payload["ok"] is True
    assert payload["resume"]["next_step"]["title"] == "read"
    assert "Continue work" in payload["resume"]["prompt"]
    assert "从上面“下一步”继续" in task_runner.render_resume(task)


def test_cli_task_continue_prints_top_level_error(monkeypatch, capsys):
    from ivyea_agent import cli, service

    monkeypatch.setattr(service, "task_continue", lambda task_id, payload: {
        "ok": False,
        "error": "model_not_configured",
    })

    rc = cli._cmd_task(Namespace(
        action="continue",
        id="task-1",
        message="",
        notes="",
        max_steps=2,
        execute=False,
    ))

    assert rc == 1
    assert "model_not_configured" in capsys.readouterr().err
