"""目标模式：一句话交出去，达成之前不停。

三件事分开验：契约（goal_store 落盘/渲染/达成判定）、验收（goal_mode 的拆解与判定）、
门禁（agent_loop 什么时候打回、什么时候放行、什么时候熔断）。
"""
from __future__ import annotations

import json

from ivyea_agent import agent_loop, budget, goal_mode, goal_store, transcript
from ivyea_agent.agent_tools import ToolContext


class FakeProvider:
    """把模型的回答写死 —— 验的是门禁的判定，不是模型的判断力。"""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls = 0
        self.last_user = ""

    def complete(self, system, user, **kw):
        self.calls += 1
        self.last_user = user
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


def _verdict(*statuses: str, achieved: bool = False, note: str = "") -> str:
    return json.dumps({
        "criteria": [{"index": i + 1, "status": st, "reason": f"r{i + 1}", "evidence": f"e{i + 1}"}
                     for i, st in enumerate(statuses)],
        "achieved": achieved, "note": note,
    }, ensure_ascii=False)


def _ctx(**kw):
    ctx = ToolContext(session_id="s1", goal_mode=True)
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def _goal(ivyea_home, *criteria: str, query: str = "把 X 修好"):
    return goal_store.start("s1", query=query, objective="把 X 修好",
                            criteria=[{"text": c, "verify": "跑一遍"} for c in criteria])


# ── 契约台账 ────────────────────────────────────────────────────────────────
def test_contract_is_persisted_and_reloadable(ivyea_home):
    _goal(ivyea_home, "测试全绿", "界面能打开")
    assert goal_store.path_for("s1").is_file()
    again = goal_store.load("s1")
    assert [c["text"] for c in again["criteria"]] == ["测试全绿", "界面能打开"]
    assert again["criteria"][0]["status"] == "pending"


def test_no_session_id_means_the_whole_module_no_ops(ivyea_home):
    """只读子 agent、裸 ToolContext 的单测走这条路，行为必须与没有这个模块时一致。"""
    assert goal_store.start("", query="x", criteria=["a"]) is None
    assert goal_store.load("") is None
    assert goal_store.render_note("") == ""


def test_a_broken_file_reads_as_no_goal(ivyea_home):
    _goal(ivyea_home, "测试全绿")
    goal_store.path_for("s1").write_text("{ 半截 JSON", encoding="utf-8")
    assert goal_store.load("s1") is None


def test_pending_criteria_are_not_achievement(ivyea_home):
    """没判过 ≠ 做到了。一条 pending 都不许算达成。"""
    goal = _goal(ivyea_home, "a", "b")
    assert goal_store.achieved(goal) is False
    goal = goal_store.record_judgment("s1", json.loads(_verdict("met")))
    assert goal_store.achieved(goal) is False          # 第二条还是 pending
    goal = goal_store.record_judgment("s1", json.loads(_verdict("met", "met")))
    assert goal_store.achieved(goal) is True
    assert goal["status"] == "achieved"


def test_unverifiable_counts_as_terminal_but_unmet_does_not(ivyea_home):
    _goal(ivyea_home, "a", "b")
    goal = goal_store.record_judgment("s1", json.loads(_verdict("met", "unverifiable")))
    assert goal_store.achieved(goal) is True
    _goal(ivyea_home, "a", "b")
    goal = goal_store.record_judgment("s1", json.loads(_verdict("met", "unmet")))
    assert goal_store.achieved(goal) is False
    assert [c["index"] for c in goal_store.unmet(goal)] == [2]


def test_judgment_only_touches_the_criteria_it_names(ivyea_home):
    """模型漏判的条目保持原状 —— 否则已经验过的东西会被反复重验。"""
    _goal(ivyea_home, "a", "b")
    goal_store.record_judgment("s1", json.loads(_verdict("met", "unmet")))
    goal = goal_store.record_judgment("s1", {"criteria": [{"index": 2, "status": "met"}]})
    assert [c["status"] for c in goal["criteria"]] == ["met", "met"]


def test_same_query_normalises_like_the_stored_one(ivyea_home):
    """比较前要走同一套归一化，否则每轮都会重新立约、把进度抹掉。"""
    goal = _goal(ivyea_home, "a", query="把  X   修好")
    assert goal_store.same_query(goal, "把 X 修好") is True
    assert goal_store.same_query(goal, "把 Y 修好") is False


def test_note_tells_the_model_it_does_not_hold_the_verdict(ivyea_home):
    _goal(ivyea_home, "测试全绿")
    note = goal_store.render_note("s1")
    assert goal_store.GOAL_NOTE_MARKER in note
    assert "判定权不在你手上" in note
    assert "还剩 1/1 条未达成" in note


def test_public_state_is_a_deterministic_projection(ivyea_home):
    _goal(ivyea_home, "a", "b")
    goal_store.record_judgment("s1", json.loads(_verdict("met", "unmet")))
    state = goal_store.public_state("s1")
    assert (state["met"], state["total"], state["status"]) == (1, 2, "active")
    assert state["criteria"][1]["status"] == "unmet"


# ── 立约与验收 ──────────────────────────────────────────────────────────────
def test_derive_falls_back_instead_of_raising_without_a_model():
    res = goal_mode.derive("把 X 修好", None)
    assert res["ok"] is False
    assert res["contract"]["criteria"]          # 兜底也必须给得出标准
    assert res["contract"]["degraded"] is True


def test_derive_survives_a_provider_blowing_up():
    class Boom:
        def complete(self, *a, **kw):
            raise RuntimeError("model down")

    res = goal_mode.derive("把 X 修好", Boom())
    assert res["ok"] is False and res["contract"]["criteria"]


def test_derive_reads_json_wrapped_in_a_code_fence():
    body = json.dumps({"objective": "修好 X",
                       "criteria": [{"text": "测试全绿", "verify": "pytest"}]}, ensure_ascii=False)
    res = goal_mode.derive("把 X 修好", FakeProvider("```json\n" + body + "\n```"))
    assert res["ok"] is True
    assert res["contract"]["criteria"][0]["text"] == "测试全绿"


def test_judge_without_a_provider_is_not_a_verdict(ivyea_home):
    goal = _goal(ivyea_home, "a")
    assert goal_mode.judge(goal, "做完了", provider=None)["ok"] is False


def test_judge_sees_the_runtime_evidence_not_just_the_answer(ivyea_home):
    goal = _goal(ivyea_home, "测试全绿")
    provider = FakeProvider(_verdict("met", achieved=True))
    goal_mode.judge(goal, "做完了", evidence=["run_tests: 12 passed"], provider=provider)
    assert "run_tests: 12 passed" in provider.last_user


def test_gate_body_tells_it_to_work_not_to_rewrite_the_summary(ivyea_home):
    _goal(ivyea_home, "a", "b")
    goal = goal_store.record_judgment("s1", json.loads(_verdict("met", "unmet")))
    body = goal_mode.gate_body(goal, {"note": "还差第二条"})
    assert "不要重写总结" in body and "直接调工具干活" in body


# ── 门禁 ────────────────────────────────────────────────────────────────────
def _status(**kw):
    st = agent_loop.TurnStatus(max_steps=10, budget=budget.TurnBudget(10))
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_gate_is_inert_when_goal_mode_is_off(ivyea_home):
    _goal(ivyea_home, "a")
    ctx = _ctx(goal_mode=False)
    assert agent_loop._goal_gate_feedback(ctx, _status(), "做完了", lambda _t: None,
                                          FakeProvider(_verdict("unmet"))) is None


def test_gate_pushes_back_while_a_criterion_is_unmet(ivyea_home):
    _goal(ivyea_home, "测试全绿", "界面能打开")
    ctx = _ctx()
    status = _status()
    fb = agent_loop._goal_gate_feedback(ctx, status, "都做完了", lambda _t: None,
                                        FakeProvider(_verdict("met", "unmet")))
    assert fb is not None
    assert fb.startswith(transcript.GOAL_GATE)
    assert status.goal_gate_rounds == 1
    assert ctx.goal_state["met"] == 1


def test_gate_releases_once_every_criterion_has_evidence(ivyea_home):
    _goal(ivyea_home, "a", "b")
    ctx = _ctx()
    assert agent_loop._goal_gate_feedback(
        ctx, _status(), "做完了", lambda _t: None,
        FakeProvider(_verdict("met", "met", achieved=True))) is None
    assert ctx.goal_state["status"] == "achieved"


def test_gate_releases_when_the_judge_itself_is_unavailable(ivyea_home):
    """验收员没上班就放行 —— 不能把用户永远关在门里。"""
    _goal(ivyea_home, "a")
    ctx = _ctx()
    notes = []
    assert agent_loop._goal_gate_feedback(ctx, _status(), "做完了", notes.append,
                                          FakeProvider("这不是 JSON")) is None
    assert any("没跑成" in n for n in notes)


def test_gate_stops_after_repeated_no_progress(ivyea_home):
    """连着几轮判定一模一样 = 原地踏步，停下汇报，别在同一处烧钱。"""
    _goal(ivyea_home, "a")
    ctx, status = _ctx(), _status()
    provider = FakeProvider(_verdict("unmet"))
    seen = [agent_loop._goal_gate_feedback(ctx, status, "x", lambda _t: None, provider)
            for _ in range(4)]
    assert seen[0] is not None                       # 第一次照常打回
    assert seen[-1] is None                          # 熔断后放行
    assert goal_store.load("s1")["status"] == "stopped"


def test_gate_stops_after_hitting_the_round_cap(ivyea_home, monkeypatch):
    _goal(ivyea_home, "a")
    ctx = _ctx()
    status = _status(goal_gate_rounds=agent_loop.GOAL_MAX_GATE_ROUNDS)
    assert agent_loop._goal_gate_feedback(ctx, status, "x", lambda _t: None,
                                          FakeProvider(_verdict("unmet"))) is None
    assert goal_store.load("s1")["status"] == "stopped"


def test_an_already_stopped_goal_does_not_re_enter_the_gate(ivyea_home):
    _goal(ivyea_home, "a")
    goal_store.stop("s1", "用户退出目标模式。")
    provider = FakeProvider(_verdict("unmet"))
    assert agent_loop._goal_gate_feedback(_ctx(), _status(), "x", lambda _t: None, provider) is None
    assert provider.calls == 0                       # 连判定都不该再花钱


# ── 预算：步数可以续，钱不能续 ───────────────────────────────────────────────
def test_renewing_steps_keeps_the_cost_meter_running():
    b = budget.TurnBudget(3, max_cost_cny=1.0)
    for _ in range(3):
        b.consume("run_command")
    b.add_cost(0.4)
    assert b.steps_exhausted() is True
    assert b.renew_steps() == 1
    assert b.steps_exhausted() is False
    assert b.cost_cny == 0.4                          # 钱一路累加，成本闸才是真刹车


def test_goal_mode_renews_the_step_budget_until_the_cost_gate_bites(ivyea_home):
    _goal(ivyea_home, "a")
    ctx = _ctx()
    b = budget.TurnBudget(1, max_cost_cny=1.0)
    b.consume("run_command")
    assert agent_loop._goal_renew_budget(ctx, _status(), b, lambda _t: None) is True
    b.add_cost(2.0)
    assert agent_loop._goal_renew_budget(ctx, _status(), b, lambda _t: None) is False
    assert goal_store.load("s1")["status"] == "stopped"


def test_an_achieved_goal_does_not_renew_anything(ivyea_home):
    _goal(ivyea_home, "a")
    goal_store.record_judgment("s1", json.loads(_verdict("met", achieved=True)))
    b = budget.TurnBudget(1)
    b.consume("run_command")
    assert agent_loop._goal_renew_budget(_ctx(), _status(), b, lambda _t: None) is False


def test_goal_mode_gets_its_own_step_budget_and_ceiling(ivyea_home):
    plain, goal = ToolContext(session_id="s1"), ToolContext(session_id="s1", goal_mode=True)
    assert agent_loop._turn_max_steps(plain, None) == agent_loop.DEFAULT_MAX_TOOL_STEPS
    assert agent_loop._turn_max_steps(goal, None) == agent_loop.GOAL_MAX_TOOL_STEPS
    assert agent_loop._turn_max_steps(goal, 42) == 42          # 调用方显式传的最大
    # 天花板要把续期的份额算进去，否则 for 循环会先于预算走完。
    assert agent_loop._goal_ceiling(goal, 10) > agent_loop._goal_ceiling(plain, 10)


# ── 注入契约：不能漏出到人眼 ────────────────────────────────────────────────
def test_the_gate_message_is_registered_as_an_injection(ivyea_home):
    """新增的门禁不登记，就会以"用户自己发过的绿气泡"出现在任务台里。"""
    _goal(ivyea_home, "a")
    goal = goal_store.record_judgment("s1", json.loads(_verdict("unmet")))
    text = transcript.gate_text(transcript.GOAL_GATE, goal_mode.gate_body(goal, {}))
    assert transcript.is_injected_user_message(text) is True
    kept = transcript.strip_injected([
        {"role": "user", "content": "把 X 修好"},
        {"role": "assistant", "content": "做完了"},
        {"role": "user", "content": text},
        {"role": "assistant", "content": "真做完了"},
    ])
    assert [m["content"] for m in kept] == ["把 X 修好", "真做完了"]


def test_the_goal_note_is_appended_to_the_last_user_message(ivyea_home):
    _goal(ivyea_home, "测试全绿")
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "把 X 修好"}]
    agent_loop._inject_goal_note(_ctx(), messages)
    assert goal_store.GOAL_NOTE_MARKER in messages[-1]["content"]
    agent_loop._inject_goal_note(_ctx(), messages)          # 幂等：不叠第二份
    assert messages[-1]["content"].count(goal_store.GOAL_NOTE_MARKER) == 1


# ── 立约的复用规则 ──────────────────────────────────────────────────────────
def test_the_same_instruction_reuses_the_contract_instead_of_re_deriving(ivyea_home):
    _goal(ivyea_home, "a", "b", query="把 X 修好")
    goal_store.record_judgment("s1", json.loads(_verdict("met", "unmet")))
    ctx = _ctx(progress_query="把 X 修好")
    provider = FakeProvider("{}")
    agent_loop._prepare_goal(ctx, _status(), provider, lambda _t: None)
    assert provider.calls == 0
    assert goal_store.load("s1")["criteria"][0]["status"] == "met"   # 进度没被抹掉


def test_saying_continue_does_not_reset_the_goal(ivyea_home):
    _goal(ivyea_home, "a", query="把 X 修好")
    ctx = _ctx(progress_query="继续")
    provider = FakeProvider("{}")
    agent_loop._prepare_goal(ctx, _status(), provider, lambda _t: None)
    assert provider.calls == 0
    assert goal_store.load("s1")["query"] == "把 X 修好"


def test_continue_revives_a_goal_that_was_stopped_by_the_budget(ivyea_home):
    """「剩余标准留在台账里，说继续可以接着做」必须是真的，不能只是一句话。"""
    _goal(ivyea_home, "a", query="把 X 修好")
    goal_store.stop("s1", "撞成本上限")
    ctx = _ctx(progress_query="继续")
    provider = FakeProvider("{}")
    agent_loop._prepare_goal(ctx, _status(), provider, lambda _t: None)
    assert provider.calls == 0
    assert goal_store.load("s1")["status"] == "active"


def test_a_new_instruction_starts_a_new_contract(ivyea_home):
    _goal(ivyea_home, "a", query="把 X 修好")
    ctx = _ctx(progress_query="改成把 Y 修好")
    body = json.dumps({"objective": "修好 Y", "criteria": [{"text": "Y 能跑", "verify": "跑一遍"}]},
                      ensure_ascii=False)
    agent_loop._prepare_goal(ctx, _status(), FakeProvider(body), lambda _t: None)
    goal = goal_store.load("s1")
    assert goal["criteria"][0]["text"] == "Y 能跑"


def test_goal_mode_does_not_switch_on_the_phase_reporting_lifecycle(ivyea_home):
    """两套仪式叠加会互相饿死。真机冒烟里 28 次调用有 19 次是记账，钱烧完了验收
    判定一次都没跑上 —— 阶段汇报开不开，仍由 routing 按这句话的性质决定。"""
    ctx = _ctx(progress_query="把 X 修好")
    body = json.dumps({"objective": "修好 X", "criteria": [{"text": "跑得通", "verify": "跑一遍"}]},
                      ensure_ascii=False)
    agent_loop._prepare_goal(ctx, _status(), FakeProvider(body), lambda _t: None)
    assert ctx.progress_required is False


# ── CLI 的自然语言入口 ──────────────────────────────────────────────────────
def test_natural_language_enters_and_leaves_goal_mode():
    from ivyea_agent.cli import _goal_mode_intent as intent
    assert intent("目标模式") == "enter"
    assert intent("进入目标模式。") == "enter"
    assert intent("退出目标模式") == "exit"
    # 长句里提到这四个字不是命令 —— 计划模式当初就是这么划的线。
    assert intent("帮我分析目标模式怎么实现") is None
    assert intent("目标模式是什么") is None


def test_one_sentence_form_carries_the_real_instruction():
    from ivyea_agent.cli import _goal_mode_prompt as prompt
    assert prompt("目标模式：把登录页的报错修好") == "把登录页的报错修好"
    assert prompt("用目标模式帮我把测试跑绿") == "帮我把测试跑绿"
    assert prompt("goal mode: fix the build") == "fix the build"
    assert prompt("讲讲目标模式") == ""          # 没有前缀就不是命令
    assert prompt("目标模式：") == ""            # 空指令交给整行匹配那条路


# ── 端到端：一轮里被打回、再交、通过 ────────────────────────────────────────
class ScriptedModel:
    """既当主脑（chat）又当验收员（complete）—— 验的是一整圈闭环怎么走。"""

    def __init__(self, replies: list[str], side: list[str]):
        self.replies = replies
        self.side = side
        self.chats = 0
        self.completes = 0
        self.seen_user = ""

    def chat(self, messages, tools=None):
        self.chats += 1
        self.seen_user = "\n".join(str(m.get("content") or "") for m in messages
                                   if m.get("role") == "user")
        return {"content": self.replies[min(self.chats - 1, len(self.replies) - 1)],
                "tool_calls": [], "usage": {}}

    def complete(self, system, user, **kw):
        self.completes += 1
        return self.side[min(self.completes - 1, len(self.side) - 1)]


def test_a_turn_is_pushed_back_until_the_goal_is_actually_met(ivyea_home):
    contract = json.dumps({"objective": "把 X 修好",
                           "criteria": [{"text": "测试全绿", "verify": "pytest"}]},
                          ensure_ascii=False)
    model = ScriptedModel(
        replies=["做完了。", "这次真跑了测试，12 passed。"],
        side=[contract, _verdict("unmet"), _verdict("met", achieved=True)],
    )
    ctx = _ctx(progress_query="把 X 修好", provider=model)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "把 X 修好"}]
    out = agent_loop.run_turn(model, ctx, messages, max_steps=8, narrate=lambda _t: None)

    assert out == "这次真跑了测试，12 passed。"      # 第一稿被打回了，交付的是第二稿
    assert model.chats == 2
    # 契约进了模型的上下文，门禁的原文也回灌了 —— 两件事缺一，循环就断在这里。
    assert goal_store.GOAL_NOTE_MARKER in model.seen_user
    assert transcript.GOAL_GATE in model.seen_user
    assert goal_store.load("s1")["status"] == "achieved"


def test_without_goal_mode_the_very_same_turn_ends_at_the_first_answer(ivyea_home):
    """对照组：同一段脚本、不开目标模式 —— 第一句"做完了"就交付了。
    这正是目标模式要消灭的那个结局。"""
    model = ScriptedModel(replies=["做完了。", "不该跑到这里"], side=["{}"])
    ctx = _ctx(goal_mode=False, progress_query="把 X 修好", provider=model)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "把 X 修好"}]
    out = agent_loop.run_turn(model, ctx, messages, max_steps=8, narrate=lambda _t: None)
    assert out == "做完了。"
    assert model.chats == 1 and model.completes == 0      # 一次判定都不该花钱


def test_a_short_new_goal_that_starts_with_continue_is_still_a_new_goal(ivyea_home):
    """「继续优化首页」是新目标，不是续做 —— 按包含匹配会让它顶着上一个目标的
    验收标准跑，那比重新立约错得离谱。"""
    assert agent_loop._is_goal_continuation("继续") is True
    assert agent_loop._is_goal_continuation("继续。") is True
    assert agent_loop._is_goal_continuation("continue") is True
    assert agent_loop._is_goal_continuation("继续优化首页") is False
    assert agent_loop._is_goal_continuation("接着把订单页也改了") is False


# ── 证据通道：判定的上限就是证据的上限 ──────────────────────────────────────
def test_the_judge_sees_the_command_text_and_its_output(ivyea_home):
    """真机冒烟栽在这里：喂给验收员的证据每条只有结果第一行，而 run_command 的
    第一行恰好是「[退出码 0]」—— 命令原文和 stdout 全被裁掉，验收员只能一遍遍判
    "只有声称、无证据"，连判三轮相同后撞上无进展熔断。"""
    from ivyea_agent.agent_tools import ToolResult
    _goal(ivyea_home, "命令输出 3")
    ctx = _ctx()
    agent_loop._note_goal_evidence(
        ctx,
        {"name": "run_command", "arguments": {"command": "python3 -c 'from add import add; print(add(1,2))'"}},
        ToolResult(True, "[退出码 0]\n3\n"))
    assert "print(add(1,2))" in ctx.goal_evidence[0]
    assert ctx.goal_evidence[0].endswith("3")          # stdout 也在

    provider = FakeProvider(_verdict("met", achieved=True))
    agent_loop._goal_gate_feedback(ctx, _status(), "跑通了", lambda _t: None, provider)
    assert "print(add(1,2))" in provider.last_user     # 真的送到了验收员手上


def test_blocked_calls_are_not_evidence_but_failures_are(ivyea_home):
    """被护栏拦下的是待办，不是证据；跑了但没过则是"这条标准没达成"的直接依据。"""
    from ivyea_agent.agent_tools import ToolResult
    _goal(ivyea_home, "测试全绿")
    ctx = _ctx()
    tc = {"name": "run_tests", "arguments": {"command": "pytest"}}
    agent_loop._note_goal_evidence(ctx, tc, ToolResult(True, "ok"), blocked=True)
    assert ctx.goal_evidence == []
    agent_loop._note_goal_evidence(ctx, tc, ToolResult(False, "[退出码 1] 2 failed"))
    assert "失败" in ctx.goal_evidence[0] and "2 failed" in ctx.goal_evidence[0]


def test_navigation_tools_are_not_evidence(ivyea_home):
    """grep/glob 是过程不是结论。全塞进去只会把真正的验证证据挤出窗口。"""
    from ivyea_agent.agent_tools import ToolResult
    _goal(ivyea_home, "a")
    ctx = _ctx()
    agent_loop._note_goal_evidence(ctx, {"name": "grep", "arguments": {"pattern": "x"}},
                                   ToolResult(True, "3 matches"))
    assert ctx.goal_evidence == []
