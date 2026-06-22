from __future__ import annotations

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
    task = task_runner.record_interruption(task["id"], "tool_step_limit", "继续时不要重复")
    assert task["status"] == "blocked"
    assert task["steps"][0]["status"] == "blocked"
    assert "不要重复" in task["steps"][0]["notes"]
    assert task["events"][-1]["kind"] == "interrupted"
