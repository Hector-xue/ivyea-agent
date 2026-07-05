"""Structured start/phase/final reporting and its runtime gates."""
from __future__ import annotations

from ivyea_agent import agent_loop, panels, progress_reporting, task_scope
from ivyea_agent.agent_tools import TOOL_SCHEMAS, ToolContext
from ivyea_agent.tools_general import t_progress_update, t_todo_write


def _plan(ctx: ToolContext) -> str:
    return t_todo_write({"todos": [
        {"content": "定位实现", "status": "in_progress"},
        {"content": "修改并验证", "status": "pending"},
    ]}, ctx)


def _start(ctx: ToolContext) -> str:
    return t_progress_update({
        "kind": "start",
        "summary": "优化分步汇报能力",
        "phase_index": 1,
        "success_criteria": ["执行前有介绍", "结束后有证据汇总"],
        "next": "先检查现有实现",
    }, ctx)


def test_complex_task_detection_is_conservative():
    assert task_scope.requires_progress_reporting("优化这个标题") is False
    assert task_scope.requires_progress_reporting("先做优化方案，再复核并分阶段执行") is True
    assert task_scope.requires_progress_reporting("详细诊断广告问题并执行优化") is True


def test_explicit_execution_cannot_silently_stop_at_a_plan():
    ctx = ToolContext()
    progress_reporting.prepare_context(
        ctx, "先复核方案，然后开始执行", required=True, execution_expected=True)
    feedback = progress_reporting.completion_feedback(ctx)
    assert "用户要求实际执行" in feedback
    assert "todo_write" in feedback


def test_progress_tool_registered():
    names = {item["function"]["name"] for item in TOOL_SCHEMAS}
    assert "progress_update" in names


def test_substantive_tool_blocked_until_plan_and_start(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path), progress_required=True)
    call = {"id": "r", "name": "read_file", "arguments": {"path": str(tmp_path / "a.py")}}
    blocked, _ = agent_loop._run_one(call, ctx)
    assert blocked.ok is False and "todo_write" in blocked.text

    _plan(ctx)
    blocked, _ = agent_loop._run_one(call, ctx)
    assert blocked.ok is False and "progress_update" in blocked.text

    assert "开始执行" in _start(ctx)
    allowed = agent_loop._guard_tool_call(ctx, call)
    assert allowed is None


def test_todo_cannot_finish_without_matching_phase_report():
    ctx = ToolContext(progress_required=True)
    _plan(ctx)
    _start(ctx)
    out = t_todo_write({"todos": [
        {"content": "定位实现", "status": "completed"},
        {"content": "修改并验证", "status": "in_progress"},
    ]}, ctx)
    assert out.startswith("⚠") and "phase_end" in out
    assert ctx.todos[0]["status"] == "in_progress"


def test_phase_and_final_report_derive_incomplete_work():
    ctx = ToolContext(progress_required=True, workspace="/tmp/demo")
    _plan(ctx)
    _start(ctx)
    ended = t_progress_update({
        "kind": "phase_end", "phase_index": 1, "status": "blocked",
        "summary": "完成入口定位，但缺少运行环境",
        "incomplete": ["未能进行真实运行验证"],
        "attention": ["需要可用的运行环境"],
    }, ctx)
    assert "blocked" in ended
    updated = t_todo_write({"todos": [
        {"content": "定位实现", "status": "blocked"},
        {"content": "修改并验证", "status": "pending"},
    ]}, ctx)
    assert "0/2" in updated

    final = t_progress_update({
        "kind": "final", "summary": "任务未全部完成",
        "incomplete": ["等待补充运行环境"],
        "attention": ["不能声称已经上线"],
    }, ctx)
    assert "执行汇总" in final
    report = ctx.progress_final
    assert any("定位实现（blocked）" in item for item in report["incomplete"])
    assert any("修改并验证（pending）" in item for item in report["incomplete"])
    assert "无" not in report["incomplete"]
    assert any("不能视为全部完成" in item for item in report["attention"])
    assert progress_reporting.completion_feedback(ctx) is None


def test_progress_panel_has_required_sections_and_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    event = {
        "kind": "final", "summary": "部分完成",
        "completed": ["完成 A"], "incomplete": ["未完成 B"],
        "evidence": ["测试 10 项通过"], "attention": ["仍需验证 B"],
    }
    rendered = panels.render_progress(event, color=True)
    assert "已做到" in rendered and "未做到" in rendered
    assert "验证" in rendered and "注意" in rendered
    assert "\033[" not in rendered


def test_plan_mode_plan_only_is_quiet_but_started_analysis_must_close():
    ctx = ToolContext(progress_required=True, plan_mode=True)
    _plan(ctx)
    assert progress_reporting.completion_feedback(ctx) is None
    _start(ctx)
    assert "phase_end" in progress_reporting.completion_feedback(ctx)


class _ReportingProvider:
    def __init__(self, root):
        self.calls = 0
        self.root = str(root)

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "final", "content": "", "usage": {}, "tool_calls": [
                {"id": "t1", "name": "todo_write", "arguments": {"todos": [
                    {"content": "完成改造", "status": "in_progress"},
                    {"content": "汇总结果", "status": "pending"},
                ]}},
                {"id": "p1", "name": "progress_update", "arguments": {
                    "kind": "start", "summary": "执行复杂优化", "phase_index": 1,
                    "success_criteria": ["阶段有证据", "最终有汇总"],
                }},
                {"id": "r1", "name": "list_dir", "arguments": {"path": self.root}},
            ]}
        elif self.calls == 2:
            yield {"type": "text", "text": "提前声称完成"}
            yield {"type": "final", "content": "提前声称完成", "usage": {}, "tool_calls": []}
        elif self.calls == 3:
            yield {"type": "final", "content": "", "usage": {}, "tool_calls": [
                {"id": "p2", "name": "progress_update", "arguments": {
                    "kind": "phase_end", "phase_index": 1, "status": "completed",
                    "summary": "完成核心改造", "completed": ["完成改造"],
                    "evidence": ["针对性测试通过"],
                }},
            ]}
        elif self.calls == 4:
            yield {"type": "final", "content": "", "usage": {}, "tool_calls": [
                {"id": "t2", "name": "todo_write", "arguments": {"todos": [
                    {"content": "完成改造", "status": "completed"},
                    {"content": "汇总结果", "status": "pending"},
                ]}},
                {"id": "p3", "name": "progress_update", "arguments": {
                    "kind": "final", "summary": "完成可执行部分",
                    "incomplete": ["汇总阶段已由结构化工具代替"],
                }},
            ]}
        else:
            yield {"type": "text", "text": "正式完成"}
            yield {"type": "final", "content": "正式完成", "usage": {}, "tool_calls": []}


def test_streaming_hides_premature_final_until_report_closes(tmp_path):
    ctx = ToolContext(workspace=str(tmp_path))
    chunks: list[str] = []
    events: list[dict] = []
    messages = [{"role": "user", "content": "先做优化方案，再复核并分阶段执行"}]
    out = agent_loop.run_turn_stream(
        _ReportingProvider(tmp_path), ctx, messages, max_steps=8,
        render=chunks.append, narrate=lambda _s: None, emit=events.append,
    )
    assert out["text"] == "正式完成"
    assert "提前声称完成" not in "".join(chunks)
    assert "正式完成" in "".join(chunks)
    assistant_texts = [block.get("text") for event in events if event["type"] == "assistant"
                       for block in event["message"]["content"] if block.get("type") == "text"]
    assert "提前声称完成" not in assistant_texts
    assert ctx.progress_final["summary"] == "完成可执行部分"
