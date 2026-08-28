"""计划台账：落盘、修订留痕、注回文本、批准闸、压缩存活。"""
from __future__ import annotations

from ivyea_agent import plan_store


def _todos(*pairs):
    return [{"content": c, "status": s} for c, s in pairs]


def test_no_session_id_is_a_noop(ivyea_home):
    """没有 session_id 的调用方（只读子 agent、裸 ToolContext）必须完全空转。"""
    assert plan_store.sync_todos("", _todos(("查资料", "pending"))) is None
    assert plan_store.render_note("") == ""
    assert plan_store.load("") is None
    assert plan_store.awaiting_approval("") is False
    assert not (plan_store.plans_dir().exists() and list(plan_store.plans_dir().iterdir()))


def test_sync_todos_persists_and_renders(ivyea_home):
    plan_store.sync_todos("s1", _todos(("盘点现状", "completed"), ("改代码", "in_progress"),
                                       ("跑测试", "pending")))
    plan = plan_store.load("s1")
    assert [s["index"] for s in plan["steps"]] == [1, 2, 3]
    assert plan["steps"][1]["started_at"] > 0     # in_progress 的步骤记了开始时间
    assert plan["steps"][0]["ended_at"] > 0       # 终态步骤记了结束时间

    note = plan_store.render_note("s1")
    assert plan_store.PLAN_NOTE_MARKER in note
    assert "✓ 1. 盘点现状" in note
    assert "▶ 2. 改代码" in note
    assert "进度 1/3" in note
    assert "当前进行中：第 2 步" in note


def test_note_points_at_next_step_when_nothing_running(ivyea_home):
    plan_store.sync_todos("s2", _todos(("A", "completed"), ("B", "pending")))
    assert "下一步应从第 2 步开始" in plan_store.render_note("s2")


def test_status_advance_is_not_a_revision(ivyea_home):
    """模型每推进一步都会重发整份 todo；那不是"改计划"，不该记成修订。"""
    plan_store.sync_todos("s3", _todos(("A", "in_progress"), ("B", "pending")))
    plan_store.sync_todos("s3", _todos(("A", "completed"), ("B", "in_progress")))
    assert plan_store.load("s3")["revisions"] == []


def test_rewriting_steps_records_a_revision(ivyea_home):
    plan_store.sync_todos("s4", _todos(("老方案", "in_progress")))
    plan_store.mark_replan("s4", "连续多步没有新证据")
    plan_store.sync_todos("s4", _todos(("新方案", "in_progress"), ("验证", "pending")))
    plan = plan_store.load("s4")
    assert len(plan["revisions"]) == 1
    assert "老方案（in_progress）" in plan["revisions"][0]["before"]
    assert plan["revisions"][0]["reason"] == "连续多步没有新证据"
    assert plan["replan_reason"] == ""     # 改写过了就不再反复要求重规划


def test_evidence_survives_a_plan_rewrite(ivyea_home):
    plan_store.sync_todos("s5", _todos(("跑测试", "in_progress")))
    plan_store.attach_evidence("s5", 1, ["pytest tests/test_x.py 通过"])
    plan_store.sync_todos("s5", _todos(("跑测试", "completed"), ("发版", "pending")))
    assert plan_store.load("s5")["steps"][0]["evidence"] == ["pytest tests/test_x.py 通过"]
    assert "证据：pytest" in plan_store.render_note("s5")


def test_replan_reason_shows_up_in_the_note(ivyea_home):
    plan_store.sync_todos("s6", _todos(("死磕同一条路", "in_progress")))
    plan_store.mark_replan("s6", "连续 8 步没有新证据")
    note = plan_store.render_note("s6")
    assert "需要重新规划：连续 8 步没有新证据" in note
    assert "todo_write 修订计划" in note


def test_plan_mode_plan_awaits_approval(ivyea_home):
    plan_store.sync_todos("s7", _todos(("A", "pending")), plan_mode=True)
    assert plan_store.awaiting_approval("s7") is True
    assert "尚未获得用户批准" in plan_store.render_note("s7")
    plan_store.approve("s7")
    assert plan_store.awaiting_approval("s7") is False
    assert "尚未获得用户批准" not in plan_store.render_note("s7")


def test_normal_chat_plan_never_awaits_approval(ivyea_home):
    """不走计划模式的普通对话不该凭空多出一道批准闸。"""
    plan_store.sync_todos("s8", _todos(("A", "pending")))
    assert plan_store.awaiting_approval("s8") is False


def test_record_start_and_reset(ivyea_home):
    plan_store.sync_todos("s9", _todos(("A", "in_progress")))
    plan_store.record_start("s9", objective="把广告巡检跑通",
                            scope=["ivyea-agent"], success_criteria=["巡检有输出", "无报错"])
    note = plan_store.render_note("s9")
    assert "目标：把广告巡检跑通" in note
    assert "完成标准：巡检有输出；无报错" in note

    plan_store.reset("s9", query="换个任务")
    assert plan_store.load("s9")["steps"] == []
    assert plan_store.render_note("s9") == ""


def test_corrupt_plan_file_is_treated_as_absent(ivyea_home):
    plan_store.sync_todos("s10", _todos(("A", "pending")))
    plan_store.path_for("s10").write_text("{ 半截 json", encoding="utf-8")
    assert plan_store.load("s10") is None
    assert plan_store.render_note("s10") == ""


def test_session_id_is_not_a_path_injection(ivyea_home):
    plan_store.sync_todos("../../etc/passwd", _todos(("A", "pending")))
    written = list(plan_store.plans_dir().iterdir())
    assert len(written) == 1
    assert written[0].parent == plan_store.plans_dir()


def test_note_is_length_capped(ivyea_home):
    plan_store.sync_todos("s11", _todos(*[(f"第 {i} 步 " + "很长的描述" * 20, "pending")
                                          for i in range(40)]))
    assert len(plan_store.render_note("s11")) <= plan_store._NOTE_MAX_CHARS + 40


# ── 计划台账 ↔ task_runner 双向对接（ADR-0026 的"还没做"项）────────────────
def _new_task(**kw):
    from ivyea_agent import task_runner
    return task_runner.create(kw.pop("title", "发布 v2"), **kw)


def test_plan_projects_into_task_steps(ivyea_home):
    """模型只调 todo_write，任务文件的步骤表也要跟着动 —— 续跑提示读的是那一份。"""
    from ivyea_agent import task_runner
    task = _new_task(steps=["旧步骤"])
    plan_store.sync_todos("s-proj", _todos(("盘点现状", "completed"), ("改代码", "in_progress")),
                          task_id=task["id"])
    got = task_runner.load(task["id"])
    assert [s["title"] for s in got["steps"]] == ["盘点现状", "改代码"]
    assert [s["status"] for s in got["steps"]] == ["completed", "in_progress"]
    assert got["status"] == "in_progress"
    # 续跑提示照着投影后的表指路，而不是任务创建时那份
    assert "改代码" in task_runner.render_resume(got)


def test_projection_carries_evidence_as_step_note(ivyea_home):
    from ivyea_agent import task_runner
    task = _new_task()
    plan_store.sync_todos("s-ev", _todos(("跑测试", "in_progress")), task_id=task["id"])
    plan_store.attach_evidence("s-ev", 1, ["pytest 76 passed"])
    assert task_runner.load(task["id"])["steps"][0]["notes"] == "pytest 76 passed"


def test_projection_completes_the_task(ivyea_home):
    from ivyea_agent import task_runner
    task = _new_task()
    plan_store.sync_todos("s-done", _todos(("A", "completed"), ("B", "skipped")), task_id=task["id"])
    assert task_runner.load(task["id"])["status"] == "completed"


def test_projection_does_not_spam_events(ivyea_home):
    """步骤没变就不该再写一次任务文件 —— 否则 events 变成 todo_write 的流水账。"""
    from ivyea_agent import task_runner
    task = _new_task()
    plan_store.sync_todos("s-quiet", _todos(("A", "pending")), task_id=task["id"])
    before = len(task_runner.load(task["id"])["events"])
    for _ in range(5):
        plan_store.sync_todos("s-quiet", _todos(("A", "pending")), task_id=task["id"])
    assert len(task_runner.load(task["id"])["events"]) == before


def test_reset_does_not_wipe_task_steps(ivyea_home):
    """换一轮查询会清空计划，但用户在任务台排的步骤不该跟着没。"""
    from ivyea_agent import task_runner
    task = _new_task()
    plan_store.sync_todos("s-reset", _todos(("A", "completed"), ("B", "pending")), task_id=task["id"])
    plan_store.reset("s-reset", task_id=task["id"], query="下一句话")
    assert len(task_runner.load(task["id"])["steps"]) == 2


def test_cancelled_task_is_not_revived(ivyea_home):
    from ivyea_agent import task_runner
    task = _new_task()
    task_runner.set_status(task["id"], "cancelled")
    plan_store.sync_todos("s-cancel", _todos(("A", "in_progress"),), task_id=task["id"])
    assert task_runner.load(task["id"])["status"] == "cancelled"


def test_missing_task_never_breaks_the_plan(ivyea_home):
    plan = plan_store.sync_todos("s-notask", _todos(("A", "pending")), task_id="20990101-does-not-exist")
    assert plan["steps"][0]["content"] == "A"     # 计划照常落盘


def test_adopt_task_seeds_an_empty_plan(ivyea_home):
    """人在任务台排好步骤 → agent 接手时计划台账要有货，[当前计划] 才注得进去。"""
    task = _new_task(title="发版 v2", steps=["跑测试", "打 tag"])
    plan = plan_store.adopt_task("s-adopt", task["id"])
    assert [s["content"] for s in plan["steps"]] == ["跑测试", "打 tag"]
    assert plan["objective"] == "发版 v2"
    note = plan_store.render_note("s-adopt")
    assert "跑测试" in note and "打 tag" in note


def test_adopt_task_never_overwrites_the_models_plan(ivyea_home):
    task = _new_task(steps=["任务台排的步骤"])
    plan_store.sync_todos("s-keep", _todos(("模型自己的计划", "in_progress")), task_id=task["id"])
    plan = plan_store.adopt_task("s-keep", task["id"])
    assert [s["content"] for s in plan["steps"]] == ["模型自己的计划"]


def test_progress_survives_reset_via_the_task_file(ivyea_home):
    """reset 清空计划后重新播种：进度靠任务文件（一直在收投影）活下来。"""
    task = _new_task(steps=["A", "B"])
    plan_store.sync_todos("s-cycle", _todos(("A", "completed"), ("B", "in_progress")), task_id=task["id"])
    plan_store.reset("s-cycle", task_id=task["id"])
    assert plan_store.load("s-cycle")["steps"] == []
    plan = plan_store.adopt_task("s-cycle", task["id"])
    assert [s["status"] for s in plan["steps"]] == ["completed", "in_progress"]


def test_adopt_task_is_a_noop_without_ids(ivyea_home):
    assert plan_store.adopt_task("", "some-task") is None
    assert plan_store.adopt_task("s-x", "") is None
    assert plan_store.load("s-x") is None
