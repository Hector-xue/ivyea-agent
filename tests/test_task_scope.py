from __future__ import annotations

from pathlib import Path

from ivyea_agent import task_scope
from ivyea_agent.agent_tools import ToolContext


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    agent = tmp_path / "ivyea-agent"
    ops = tmp_path / "ivyea-ops"
    for root in (agent, ops):
        (root / ".git").mkdir(parents=True)
        (root / "README.md").write_text(root.name, encoding="utf-8")
    return agent, ops


def test_current_explicit_project_beats_screenshot_host_and_history(tmp_path):
    agent, ops = _repos(tmp_path)
    messages = [
        {"role": "user", "content": "之前检查 ivyea-ops 页面"},
        {"role": "assistant", "content": "好的"},
    ]
    result = task_scope.resolve(
        "截图来自 ops.ivyea.com/terminal，但这是 ivyeaagent 的输出任务",
        tmp_path,
        messages=messages,
        locked_root=str(ops),
    )
    assert result.root == str(agent)
    assert result.explicit is True
    assert result.confidence == "explicit"
    assert result.visual is True


def test_recent_user_target_is_retained_for_ambiguous_screenshot_followup(tmp_path):
    agent, _ops = _repos(tmp_path)
    current = "@/tmp/screen.jpg 这种彩色输出能做吗？"
    messages = [
        {"role": "user", "content": "检查 ivyea-agent 的安装输出"},
        {"role": "assistant", "content": "已检查"},
        {"role": "user", "content": current},
    ]
    result = task_scope.resolve(current, tmp_path, messages=messages)
    assert result.root == str(agent)
    assert result.confidence == "history"
    assert "最近用户上下文" in result.evidence[0]


def test_explicit_new_project_switches_existing_lock(tmp_path):
    agent, ops = _repos(tmp_path)
    result = task_scope.resolve("现在改 ivyeaops 的前端", tmp_path, locked_root=str(agent))
    assert result.root == str(ops)
    assert result.explicit is True


def test_negated_project_mention_does_not_create_false_ambiguity(tmp_path):
    agent, _ops = _repos(tmp_path)
    result = task_scope.resolve("你找错方向了，跟ivyeaops没关系，这是ivyeaagent的任务", tmp_path)
    assert result.ambiguous is False
    assert result.root == str(agent)
    assert result.explicit is True


def test_two_explicit_projects_are_ambiguous_and_block_lock(tmp_path):
    _repos(tmp_path)
    result = task_scope.resolve("比较并同时修改 ivyea-agent 和 ivyea-ops", tmp_path)
    assert result.ambiguous is True
    assert result.root == ""
    assert "先向用户确认" in task_scope.render_note(result, "比较两个项目")


def test_prepare_query_locks_tool_workspace_and_adds_behavior_contract(tmp_path):
    agent, _ops = _repos(tmp_path)
    ctx = ToolContext(workspace=str(tmp_path))
    note = task_scope.prepare_query(ctx, "优化 ivyeaagent 的终端颜色", [], base=tmp_path)
    assert ctx.workspace == str(agent)
    assert ctx.target_root == str(agent)
    assert ctx.behavioral_task is True
    assert "工具搜索根" in note
    assert "真实运行路径" in note


def test_irrelevant_chat_does_not_keep_injecting_scope_contract(tmp_path):
    agent, _ops = _repos(tmp_path)
    ctx = ToolContext(workspace=str(tmp_path))
    task_scope.prepare_query(ctx, "优化 ivyeaagent 的终端颜色", [], base=tmp_path)
    assert ctx.target_root == str(agent)
    assert task_scope.prepare_query(ctx, "你好", [], base=tmp_path) == ""


def test_continuation_retains_behavioral_contract_and_resets_explicit_search_deadend(tmp_path):
    agent, _ops = _repos(tmp_path)
    ctx = ToolContext(workspace=str(tmp_path), behavioral_task=True,
                      search_recovery_required=True, consecutive_search_deadends=2,
                      navigation_since_read=8)
    note = task_scope.prepare_query(ctx, "继续优化 ivyeaagent", [], base=tmp_path)
    assert ctx.target_root == str(agent)
    assert ctx.behavioral_task is True
    assert ctx.search_recovery_required is False
    assert ctx.navigation_since_read == 0
    assert "真实运行路径" in note


def test_prepare_messages_supports_multimodal_content_without_duplicate_note(tmp_path):
    agent, _ops = _repos(tmp_path)
    ctx = ToolContext(workspace=str(tmp_path))
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "这是 ivyeaagent 的截图输出"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
    ]}]
    task_scope.prepare_messages(ctx, messages)
    task_scope.prepare_messages(ctx, messages)
    text = "\n".join(str(row.get("text") or "") for row in messages[0]["content"] if isinstance(row, dict))
    assert ctx.target_root == str(agent)
    assert text.count(task_scope.SCOPE_MARKER) == 1


def test_explicit_lock_is_not_silently_replaced_by_reading_other_repo(tmp_path):
    agent, ops = _repos(tmp_path)
    ctx = ToolContext(workspace=str(agent), target_root=str(agent), target_project=agent.name,
                      target_explicit=True)
    adopted = task_scope.adopt_project_from_path(ctx, ops / "README.md")
    assert adopted == ""
    assert ctx.target_root == str(agent)


# ── 复杂度判据只看用户说的话 ──────────────────────────────────────────────


def test_injected_knowledge_does_not_make_a_simple_task_complex():
    """**注入的检索证据不算用户说的话。**

    真事：用户在任务台打了 28 个字「把 UK 站某活动的日预算改成 8」，知识检索往后面
    追加了 3851 字的证据块（里面自带"执行/分析/诊断"这些词）。复杂度判据拿整段去判，
    命中"动作词 + 长度≥60"，把一步操作判成复杂多步任务 —— 于是整套 todo + 阶段汇报
    状态机启动，同一个动作被拦两轮，来回几十条消息，用户等了 8 分钟才看到结果。
    """
    from ivyea_agent.task_scope import requires_progress_reporting

    plain = "把 UK 站『绿植零号手动』这个广告活动的日预算改成 8"
    injected = plain + """

[Ivyea 本地知识检索 / 亚马逊知识证据]
检索决策：risk=medium reason=amazon_domain_question。
[K1] 2026年7月利润-提成.xlsx | authority=account_local
     执行摘要：分析各 ASIN 的利润与提成，诊断异常项并给出处理建议……
[K2] 广告优化方法论 | 执行前先分析大盘，再逐条诊断关键词表现……
"""
    assert requires_progress_reporting(plain) is False
    assert requires_progress_reporting(injected) is False, "注入的证据把简单任务撑成了复杂任务"


def test_really_complex_work_still_triggers_reporting():
    """别为了修上面那个把纪律整个关掉 —— 真的多步工程仍然要走汇报闭环。"""
    from ivyea_agent.task_scope import requires_progress_reporting

    assert requires_progress_reporting(
        "帮我把这个仓库的广告模块重构一遍，先分析现状再制定方案然后逐步实施") is True


def test_user_said_strips_every_known_injection_marker():
    from ivyea_agent.task_scope import _user_said

    for marker in ("[Ivyea 本地知识检索 / 亚马逊知识证据]", "[知识引用门禁]",
                   "[用户显式引用的资料 —— 优先据此作答]", "[角色设定 —— 按这个身份作答]",
                   # 这两条曾经漏掉：模块里有过**两份同名清单**，下面那份把上面那份
                   # 整个遮蔽，而它恰好没有技能这一条。见 task_scope._INJECTED_MARKERS。
                   "[Ivyea Skill：本轮相关可复用流程]", "[工程上下文]",
                   "[Ivyea 内置亚马逊知识库：本轮相关摘录]", "[任务范围锁定 / 执行契约]"):
        assert _user_said(f"改一下预算\n\n{marker}\n一堆注入内容") == "改一下预算"


def test_injection_markers_cover_every_real_injection_site():
    """清单必须覆盖**代码里真的会拼到用户消息后面**的每一个块头。

    只测"清单里的都能切"是不够的 —— 上一次出事正是因为某个真实注入点根本不在
    清单里，而它照样通过了那条用例。这里反过来，从源码里把注入点抓出来对账。
    """
    import re
    from pathlib import Path
    from ivyea_agent import task_scope

    root = Path(task_scope.__file__).parent
    headers: set[str] = set()
    for name in ("service.py", "cli.py"):
        src = (root / name).read_text(encoding="utf-8")
        # 只看**拼进用户消息**的那些（system += 是系统提示，判据本来就看不到它）。
        # 形如  user_content += "\n\n[某某块头]\n" ...
        headers |= set(re.findall(
            r'\b(?:user_content|message|content|user_msg)\s*\+=\s*\(?\s*"\\n\\n(\[[^"\\]{2,40})', src))
    assert headers, "没抓到任何注入点，正则该跟着代码更新"
    # 例外：这一块拼的是**用户自己补的那句话**（续跑任务时的追加指令），
    # 不是系统上下文。它必须留在判据里 —— 切掉就等于没听见用户刚说的话。
    user_speech = {"[本轮补充要求]"}
    lowered = tuple(m.lower() for m in task_scope._INJECTED_MARKERS)
    for head in headers - user_speech:
        assert any(head.lower().startswith(m) or m in head.lower() for m in lowered), (
            f"注入块 {head!r} 不在 _INJECTED_MARKERS 里 —— 它会被当成用户说的话，"
            f"把复杂度判据带偏"
        )


def test_short_command_stays_simple_even_with_skill_injected():
    """一句"测试"被自动匹配的技能手册撑到 1600 字，仍然是简单任务。

    实测事故：这条判成复杂 → 护栏要求先 todo_write + progress_update → 模型
    18 步里 17 步在写待办和阶段汇报，一个词等了 2 分 16 秒。
    """
    from ivyea_agent import skills
    from ivyea_agent.task_scope import requires_progress_reporting

    sctx, _ = skills.context_for_query("测试", limit=2)
    injected = "测试\n\n[Ivyea Skill：本轮相关可复用流程]\n" + (sctx or "")
    assert len(injected) > 200, "这条用例的前提就是注入块很长"
    assert requires_progress_reporting(injected) is False
