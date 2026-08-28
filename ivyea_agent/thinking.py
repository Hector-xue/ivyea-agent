"""思考深度：按这一轮的性质决定想多深。

为什么要有这一层
----------------
`reasoning_effort` 此前是一个**全局静态旋钮**，默认 `high`：一句"好的"和一次跨仓重构
按同样的深度去想。`routing.py` 早就把每轮判成了 chat / board / work 三条路线，
那份判断只被用来决定"挂不挂工具"，没人拿它调思考深度 —— 白算了。

这里补上：新增一档 **`adaptive`**，按路线和任务性质推导本轮的思考深度。

为什么是新增一档，而不是重新定义 `auto`
--------------------------------------
`auto` **已经有明确含义了**，而且各家不一样：Gemini 把它映射成 `thinkingBudget: -1`
（动态思考，模型自己决定），其余几家映射成"不传思考参数、用模型默认"。把 `auto`
改成"自适应"会**悄悄改掉所有显式设过 auto 的人的行为**，而他们当初选它正是为了
"交给模型自己定"。所以另开一档，`auto` 一个字节不动。

默认仍然是 `high`
-----------------
不做任何设置的老用户行为**逐字不变**。`adaptive` 是显式选项，`/think adaptive` 或
`ivyea config set reasoning_effort adaptive` 才生效。这条是硬要求：思考深度直接影响
回答质量，不能因为"我觉得这样更省"就替用户降档。
"""
from __future__ import annotations

from typing import Any

#: `/think` 与配置接受的全部档位。`adaptive` 排在最后 —— 它不是一个深度，是一条规则。
LEVELS = ("off", "low", "medium", "high", "auto", "adaptive")

ADAPTIVE = "adaptive"
DEFAULT_EFFORT = "high"

#: 自适应映射。取值刻意保守：**唯一被调低的是寒暄**，其余一律维持默认的 high。
#:
#: 为什么不顺手把"普通工作轮"降到 medium：判不准的轮次一律落在 work（见 routing.py
#: 的设计），而"判不准"里混着大量真需要想的活。降它省下的是零头，赔上的是回答质量。
_LANE_EFFORT = {
    "chat": "low",      # 寒暄/问身份/简单常识：这条路线连工具都不挂，思考预算同样没必要
    "board": "high",    # 板块任务：一次长工具调用，但结论仍要人看，不降
    "work": "high",
}


def resolve(setting: str, *, lane: str = "work", progress_required: bool = False,
            execution_expected: bool = False) -> str:
    """本轮实际用的思考深度。

    `setting` 不是 `adaptive` 时**原样返回** —— 用户显式选的档位是命令，不是建议。
    """
    eff = (setting or "").strip().lower()
    if eff != ADAPTIVE:
        return eff if eff in LEVELS else DEFAULT_EFFORT
    if progress_required or execution_expected:
        return "high"      # 多步执行任务：无论哪条路线都往高了想
    return _LANE_EFFORT.get((lane or "work").lower(), "high")


def resolve_for_context(ctx: Any, setting: str | None = None) -> str:
    """从 ToolContext 上读出判据并解析。设置读不到时按默认档，绝不抛。"""
    if setting is None:
        try:
            from . import config
            setting = str(config.get_setting("reasoning_effort", DEFAULT_EFFORT) or DEFAULT_EFFORT)
        except Exception:   # noqa: BLE001
            setting = DEFAULT_EFFORT
    return resolve(
        setting,
        lane=str(getattr(ctx, "route_lane", "") or "work"),
        progress_required=bool(getattr(ctx, "progress_required", False)),
        execution_expected=bool(getattr(ctx, "progress_execution_expected", False)),
    )


def apply_to(provider: Any, ctx: Any) -> str:
    """把本轮该用的深度挂到 provider 上，返回实际生效的档位。

    provider 每个会话只构造一次（`from_settings` 在建会话时跑），所以自适应必须在
    **每轮开始时**改 `provider.reasoning_effort`，不能挂在构造那一步。

    共享对象的隐患
    --------------
    `provider` 是**整条会话共用的一个对象**，子 agent 也是拿它去跑。所以这个赋值是在改
    共享状态：子 agent 那一轮会把主线的档位覆盖掉，而且并行派发时是多个线程同时写同一个
    属性。今天两边解析出来恰好都是 `high`（子 agent 的 ctx 没有 route_lane，落到 work），
    所以还没出过事 —— 但那是巧合，不是设计。

    所以这里加两道：**子 agent 的轮次不碰 provider**（它继承主线这一轮的档位就够了），
    以及赋值前先看值是否真的变了，不变就不写。
    """
    if provider is None:
        return ""
    effort = resolve_for_context(ctx)
    # 子 agent / 后台沉淀这类"内部轮次"不改共享 provider：它们没有自己的路线判断，
    # 改了只会把主线刚定好的档位覆盖掉。
    if getattr(ctx, "progress_reporting_disabled", False):
        try:
            ctx.thinking_effort = str(getattr(provider, "reasoning_effort", "") or "")
        except Exception:   # noqa: BLE001
            pass
        return str(getattr(provider, "reasoning_effort", "") or "")
    try:
        if getattr(provider, "reasoning_effort", None) != effort:
            provider.reasoning_effort = effort
    except Exception:   # noqa: BLE001 —— 挂不上去就用 provider 自己的默认，不打断这一轮
        return ""
    try:
        ctx.thinking_effort = effort
    except Exception:   # noqa: BLE001
        pass
    return effort
