"""思考深度自适应：只在 adaptive 档生效，其余一律照旧。"""
from __future__ import annotations

from ivyea_agent import agent_loop, thinking
from ivyea_agent.agent_tools import ToolContext


class FakeProvider:
    reasoning_effort = "high"


def test_explicit_levels_pass_through_untouched():
    """用户显式选的档位是命令不是建议 —— 任何路线都不许改它。"""
    for level in ("off", "low", "medium", "high", "auto"):
        for lane in ("chat", "board", "work"):
            assert thinking.resolve(level, lane=lane) == level
            assert thinking.resolve(level, lane=lane, progress_required=True) == level


def test_auto_is_not_adaptive():
    """auto 各家已有含义（Gemini 是动态思考预算），绝不能被 adaptive 顶掉。"""
    assert thinking.resolve("auto", lane="chat") == "auto"
    assert thinking.ADAPTIVE != "auto"
    assert "auto" in thinking.LEVELS and "adaptive" in thinking.LEVELS


def test_adaptive_only_lowers_small_talk():
    assert thinking.resolve("adaptive", lane="chat") == "low"
    assert thinking.resolve("adaptive", lane="board") == "high"
    assert thinking.resolve("adaptive", lane="work") == "high"


def test_adaptive_keeps_high_for_multi_step_execution():
    """多步执行任务：哪怕被判成寒暄路线也往高了想（判错的代价不对称）。"""
    assert thinking.resolve("adaptive", lane="chat", progress_required=True) == "high"
    assert thinking.resolve("adaptive", lane="chat", execution_expected=True) == "high"


def test_unknown_setting_falls_back_to_the_default():
    assert thinking.resolve("阴间档位", lane="work") == thinking.DEFAULT_EFFORT
    assert thinking.resolve("", lane="work") == thinking.DEFAULT_EFFORT


def test_apply_to_sets_the_provider_knob(ivyea_home):
    from ivyea_agent import config
    config.set_setting("reasoning_effort", "adaptive")
    provider = FakeProvider()
    ctx = ToolContext(workspace=".", route_lane="chat")
    assert thinking.apply_to(provider, ctx) == "low"
    assert provider.reasoning_effort == "low"
    assert ctx.thinking_effort == "low"


def test_default_setting_changes_nothing(ivyea_home):
    """不做任何设置的老用户：每一条路线都还是 high。"""
    provider = FakeProvider()
    for lane in ("chat", "board", "work"):
        assert thinking.apply_to(provider, ToolContext(workspace=".", route_lane=lane)) == "high"
        assert provider.reasoning_effort == "high"


def test_apply_to_tolerates_a_frozen_provider():
    class Frozen:
        __slots__ = ()
    assert thinking.apply_to(Frozen(), ToolContext(workspace=".")) == ""
    assert thinking.apply_to(None, ToolContext(workspace=".")) == ""


def test_turn_loop_applies_thinking(ivyea_home, monkeypatch):
    """接线验证：跑一轮就会按路线把旋钮拧好。"""
    from ivyea_agent import config
    config.set_setting("reasoning_effort", "adaptive")

    class OneShot(FakeProvider):
        def chat(self, messages, tools=None, **kw):
            return {"role": "assistant", "content": "好的", "tool_calls": []}

    provider = OneShot()
    ctx = ToolContext(workspace=".", session_id="think-1", route_lane="chat")
    monkeypatch.setattr(agent_loop.config, "get_setting",
                        lambda k, d=None: "adaptive" if k == "reasoning_effort" else d)
    agent_loop.run_turn(provider, ctx, [{"role": "user", "content": "你好"}],
                        max_steps=2, narrate=lambda _s: None)
    assert provider.reasoning_effort == "low"


# ── 共享 provider（复核时发现的潜在问题） ───────────────────────────────────
def test_a_subagent_turn_does_not_clobber_the_main_lane_setting(ivyea_home):
    """provider 是整条会话共用的一个对象，子 agent 也拿它去跑。
    子 agent 那一轮不该把主线刚定好的档位覆盖掉。"""
    from ivyea_agent import config
    config.set_setting("reasoning_effort", "adaptive")
    provider = FakeProvider()

    main = ToolContext(workspace=".", route_lane="chat")
    assert thinking.apply_to(provider, main) == "low"

    # 子 agent：progress_reporting_disabled=True，没有自己的路线判断
    sub = ToolContext(workspace=".", progress_reporting_disabled=True)
    assert thinking.apply_to(provider, sub) == "low"      # 继承，不改写
    assert provider.reasoning_effort == "low"             # 主线的设定还在
    assert sub.thinking_effort == "low"


def test_apply_to_is_a_no_op_when_the_value_is_unchanged(ivyea_home):
    """并行派发时多个线程会写同一个属性；值没变就干脆不写。"""
    class Counting:
        def __init__(self):
            self._v = "high"
            self.writes = 0

        @property
        def reasoning_effort(self):
            return self._v

        @reasoning_effort.setter
        def reasoning_effort(self, value):
            self.writes += 1
            self._v = value

    provider = Counting()
    ctx = ToolContext(workspace=".", route_lane="work")
    for _ in range(5):
        thinking.apply_to(provider, ctx)          # 默认档 high，与现值相同
    assert provider.writes == 0
