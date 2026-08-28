"""收尾自查门禁：只盯"动过东西"的轮次，拿不准一律放行。"""
from __future__ import annotations

from ivyea_agent import agent_loop, critique
from ivyea_agent.agent_tools import ToolContext, ToolResult


class Critic:
    """把复核结论写死，用来验门禁的判定而不是模型的判断力。"""

    def __init__(self, verdict: str):
        self.verdict = verdict
        self.calls = 0
        self.last_user = ""

    def complete(self, system, user, **kw):
        self.calls += 1
        self.last_user = user
        return self.verdict


def _status(**kw):
    st = agent_loop.TurnStatus(max_steps=10)
    for k, v in kw.items():
        setattr(st, k, v)
    return st


# ── needs_fix 的判定方向：宁可少拦 ────────────────────────────────────────────
def test_needs_fix_reads_the_verdict_line():
    assert critique.needs_fix("1. 这里漏了边界\n\n**建议修正**：补上空值分支") is True
    assert critique.needs_fix("没发现问题。\n\n**通过**") is False
    assert critique.needs_fix("") is False


def test_ambiguous_verdict_is_treated_as_pass():
    """误判成"要修"的代价是白跑一轮甚至改坏对的答案；误判成"通过"只是这次没帮上忙。"""
    assert critique.needs_fix("正文里提到了建议修正这个词\n\n**通过**") is False


def test_rubrics_are_domain_specific():
    assert "消费方契约" in critique.pick_rubric("code")
    assert "归因销售不等于增量销售" in critique.pick_rubric("ads")
    assert "官方事实" in critique.pick_rubric("knowledge")
    assert critique.pick_rubric("没这个域") == critique.DEFAULT_RUBRIC


# ── 触发面：只盯动过东西的轮次 ───────────────────────────────────────────────
def test_plain_answer_turn_is_never_critiqued(ivyea_home):
    critic = Critic("**建议修正**：随便挑个刺")
    ctx = ToolContext(workspace=".", provider=critic)
    assert agent_loop._critique_gate_feedback(ctx, _status(), "答案", lambda _s: None) is None
    assert critic.calls == 0


def test_doc_only_turn_is_never_critiqued(ivyea_home):
    """只写了 .md 的轮次不算"动过东西" —— 与行为门禁同一条线。"""
    critic = Critic("**建议修正**：随便挑个刺")
    ctx = ToolContext(workspace=".", provider=critic)
    st = _status()
    st.observe_tool_result("write_file", ToolResult(True, "已写入"), {"path": "报告.md"})
    assert st.wrote_code and not st.wrote_code_files
    assert agent_loop._critique_gate_feedback(ctx, st, "答案", lambda _s: None) is None
    assert critic.calls == 0


def test_code_turn_is_critiqued_and_blocked_on_findings(ivyea_home):
    critic = Critic("1. 没验证过\n\n**建议修正**：跑一遍再说")
    ctx = ToolContext(workspace=".", provider=critic, session_id="crit-1")
    st = _status(wrote_code=True, wrote_code_files=True)
    fb = agent_loop._critique_gate_feedback(ctx, st, "改完了", lambda _s: None)
    assert fb is not None
    assert "收尾自查" in fb and "跑一遍再说" in fb
    assert st.critique_rounds == 1
    assert "消费方契约" in critic.last_user      # 用了 code 那套维度


def test_a_passing_critique_is_silent(ivyea_home):
    critic = Critic("**通过**")
    ctx = ToolContext(workspace=".", provider=critic)
    st = _status(wrote_code=True, wrote_code_files=True)
    assert agent_loop._critique_gate_feedback(ctx, st, "改完了", lambda _s: None) is None
    assert critic.calls == 1


def test_real_ad_writes_use_the_ads_rubric(ivyea_home):
    critic = Critic("**通过**")
    ctx = ToolContext(workspace=".", provider=critic)
    ctx.executed_writes = True
    agent_loop._critique_gate_feedback(ctx, _status(), "已执行", lambda _s: None)
    assert "归因销售不等于增量销售" in critic.last_user


def test_the_gate_runs_at_most_once(ivyea_home):
    critic = Critic("**建议修正**：还是有问题")
    ctx = ToolContext(workspace=".", provider=critic)
    st = _status(wrote_code=True, wrote_code_files=True)
    assert agent_loop._critique_gate_feedback(ctx, st, "第一稿", lambda _s: None) is not None
    assert agent_loop._critique_gate_feedback(ctx, st, "第二稿", lambda _s: None) is None
    assert critic.calls == 1


def test_model_self_critique_skips_the_runtime_one(ivyea_home):
    critic = Critic("**建议修正**：随便挑个刺")
    ctx = ToolContext(workspace=".", provider=critic)
    st = _status(wrote_code=True, wrote_code_files=True)
    st.observe_tool_result("self_critique", ToolResult(True, "查过了，没问题"))
    assert st.self_critiqued is True
    assert agent_loop._critique_gate_feedback(ctx, st, "改完了", lambda _s: None) is None
    assert critic.calls == 0


def test_the_switch_turns_it_off(ivyea_home):
    from ivyea_agent import config
    config.set_setting("critique_before_done", False)
    critic = Critic("**建议修正**：随便挑个刺")
    ctx = ToolContext(workspace=".", provider=critic)
    st = _status(wrote_code=True, wrote_code_files=True)
    assert agent_loop._critique_gate_feedback(ctx, st, "改完了", lambda _s: None) is None
    assert critic.calls == 0


def test_a_broken_critic_never_blocks_delivery(ivyea_home):
    class Broken:
        def complete(self, *a, **kw):
            raise RuntimeError("模型挂了")

    ctx = ToolContext(workspace=".", provider=Broken())
    st = _status(wrote_code=True, wrote_code_files=True)
    assert agent_loop._critique_gate_feedback(ctx, st, "改完了", lambda _s: None) is None


def test_no_provider_no_gate(ivyea_home):
    ctx = ToolContext(workspace=".", provider=None)
    st = _status(wrote_code=True, wrote_code_files=True)
    assert agent_loop._critique_gate_feedback(ctx, st, "改完了", lambda _s: None) is None


# ── 通过时的旁注：付了钱就要拿到东西 ─────────────────────────────────────────
def test_a_passing_critique_still_surfaces_its_findings(ivyea_home):
    """实测里一次判"通过"的复核仍然指出了真问题（调用方依赖 ZeroDivisionError）。
    静默丢掉等于白付一次模型调用。"""
    verdict = ("1. **需求吻合**：未发现超范围修改。\n"
               "2. **消费方契约**：未检查调用方是否依赖 ZeroDivisionError 而非 ValueError。\n"
               "3. **失败路径**：未说明 b 为 -0.0 时是否同样触发保护。\n\n**通过**")
    seen = []
    ctx = ToolContext(workspace=".", provider=Critic(verdict))
    st = _status(wrote_code=True, wrote_code_files=True)
    assert agent_loop._critique_gate_feedback(ctx, st, "改完了", seen.append) is None
    joined = "\n".join(seen)
    assert "自查备注" in joined
    assert "ZeroDivisionError" in joined
    assert "-0.0" in joined
    assert st.critique_rounds == 0          # 备注不是门禁，不占修正轮次


def test_the_note_drops_empty_findings_and_the_verdict_line():
    md = ("1. 需求吻合：未发现问题。\n2. 事实可靠：无异常。\n"
          "3. 关键遗漏：漏了并发下的竞态。\n\n**通过**")
    note = agent_loop._critique_note(md)
    assert "并发下的竞态" in note
    assert "未发现问题" not in note and "通过" not in note


def test_a_clean_critique_produces_no_note():
    assert agent_loop._critique_note("**通过**") == ""
    assert agent_loop._critique_note("") == ""


def test_the_note_is_capped():
    md = "\n".join(f"{i}. 第 {i} 条发现，" + "很长的描述" * 40 for i in range(1, 9))
    note = agent_loop._critique_note(md)
    assert len(note.splitlines()) == agent_loop._CRITIQUE_NOTE_ITEMS
    assert all(len(line) <= agent_loop._CRITIQUE_NOTE_CHARS + 6 for line in note.splitlines())
