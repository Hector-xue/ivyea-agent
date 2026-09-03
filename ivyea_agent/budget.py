"""一轮的预算：步数 + 成本。

为什么步数不能裸着数
--------------------
`routing.py` 顶上记着一组实测：**最近 300 次工具调用里 112 次（37%）是纯记账调用**
（`progress_update` / `todo_write`），一句"测试"跑了 18 步、其中 17 步是记账，工具自身
耗时 0.0s，用户等了 2 分 16 秒。这些调用不产生任何进展，却和真正干活的调用一样吃步数配额。

于是"步数上限"量的不是"这个任务有多大"，而是"模型有多爱记账"。这里把记账调用**退款**：
它们照常执行、照常展示，只是不计进预算。真正会跑飞的是干活的调用，配额也就该只管它们。

为什么要有成本闸
----------------
步数上限是"防跑飞"，不是"防烧钱"——同样 200 步，读几个小文件和反复喂 20 万 token 的上下文
差着两个数量级。此前唯一的止损点是步数，于是一轮可以安安静静烧掉很多钱才撞上限。
成本闸**默认关**（`chat_max_cost_cny` 不设就没有），设了才在越线时停下来汇报。

两条纪律
--------
* **到了上限是停下来汇报，不是抛异常。** 走既有的"步数用尽"收尾路径，把已做到/未做到
  和续跑提示给用户。
* **子 agent 有自己的预算，不吃主线额度。** 派三个子 agent 去查三件事，不该让主线只剩
  三分之一的配额。
"""
from __future__ import annotations

import threading

#: 不计进步数预算的工具：它们只维护状态、不推进任务。
#: 与 `progress_reporting.META_TOOLS` 同义，独立写一份避免反向依赖。
REFUNDED_TOOLS = frozenset({
    "progress_update", "todo_write", "self_critique",
    "task_read", "task_step", "task_log", "task_resume",
})


class TurnBudget:
    """一轮的配额。线程安全 —— 只读工具会被并行派发。"""

    def __init__(self, max_steps: int, max_cost_cny: float = 0.0) -> None:
        self.max_steps = max(1, int(max_steps))
        self.max_cost_cny = max(0.0, float(max_cost_cny or 0.0))
        self._lock = threading.Lock()
        self.steps_used = 0          # 计进预算的调用数
        self.steps_refunded = 0      # 记账调用数（照常执行，不占配额）
        self.cost_cny = 0.0
        self.hit_ceiling = False     # 是不是撞模型步数天花板停的（≠ 预算用完）
        self.renewals = 0            # 步数配额续过几次（目标模式的自动续跑）

    # ── 步数 ────────────────────────────────────────────────────────────────
    def consume(self, tool_name: str = "") -> None:
        """记一次工具调用。记账类工具退款（不占配额）。"""
        with self._lock:
            if tool_name in REFUNDED_TOOLS:
                self.steps_refunded += 1
            else:
                self.steps_used += 1

    @property
    def steps_remaining(self) -> int:
        return max(0, self.max_steps - self.steps_used)

    def steps_exhausted(self) -> bool:
        return self.steps_used >= self.max_steps

    # ── 成本 ────────────────────────────────────────────────────────────────
    def add_cost(self, cny: float) -> float:
        with self._lock:
            self.cost_cny += max(0.0, float(cny or 0.0))
            return self.cost_cny

    def cost_exhausted(self) -> bool:
        return bool(self.max_cost_cny) and self.cost_cny >= self.max_cost_cny

    def exhausted(self) -> bool:
        return self.steps_exhausted() or self.cost_exhausted()

    def renew_steps(self) -> int:
        """把步数配额重新加满，返回这是第几次续期。**成本不清零。**

        目标模式专用：用户按下那个开关的意思就是"达成之前别停"，而步数上限本来只是
        防跑飞的安全阀 —— 撞上它就收摊，等于把"不达成不停"改成"跑 600 步就停"。
        所以步数可以续，钱不能续：`cost_cny` 一路累加，成本闸因此始终是真正的刹车。
        """
        with self._lock:
            self.steps_used = 0
            self.renewals += 1
            return self.renewals

    def mark_ceiling(self) -> None:
        """标记这一轮是撞**模型步数天花板**停的，而不是把预算用完了。

        两者要分开：预算用完 = 真的干了这么多活，说"继续"就接着做；撞天花板 =
        绝大多数步数花在记账上、预算根本没动，这时候叫用户去调 `chat_max_tool_steps`
        是**帮倒忙**（预算从来不是瓶颈）。
        """
        self.hit_ceiling = True

    def stop_reason(self) -> str:
        """为什么停。空串=没停。"""
        if self.cost_exhausted():
            return "cost"
        if self.steps_exhausted():
            return "steps"
        if getattr(self, "hit_ceiling", False):
            return "ceiling"
        return ""

    def render(self) -> str:
        parts = [f"{self.steps_used}/{self.max_steps} 步"]
        if self.renewals:
            parts.append(f"已续跑 {self.renewals} 轮配额")
        if self.steps_refunded:
            parts.append(f"另有 {self.steps_refunded} 次记账调用未计入")
        if self.max_cost_cny:
            parts.append(f"¥{self.cost_cny:.4f}/¥{self.max_cost_cny:.2f}")
        elif self.cost_cny:
            parts.append(f"¥{self.cost_cny:.4f}")
        return "，".join(parts)


def from_settings(max_steps: int | None = None, *, steps_key: str = "chat_max_tool_steps",
                  default_steps: int = 200, cost_key: str = "chat_max_cost_cny") -> TurnBudget:
    """按配置造一轮的预算。`max_steps` 显式传了就以它为准（调用方最清楚）。"""
    from . import config

    def _num(key: str, default):
        try:
            return type(default)(config.get_setting(key, default))
        except (TypeError, ValueError):
            return default

    steps = int(max_steps) if max_steps is not None else max(1, int(_num(steps_key, default_steps)))
    return TurnBudget(max(1, steps), _num(cost_key, 0.0))
