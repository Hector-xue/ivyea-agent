"""打转守卫：重复调用 + 无进展检测。

`agent_loop` 此前只有两条**专项**守卫：导航 8 次没读文件、搜索返回 0 文件后必须先
`list_dir`。它们只管"找文件"这一件事。真正会把一轮烧到步数上限的，是更朴素的两种情况：

* **原地重复**：同一个工具、同一组参数，一连调三五次，每次拿回一模一样的失败。
  模型不会自己意识到"我刚才就这么试过"——它只看得到上一条工具结果，看不到"这已经是
  第四遍了"。
* **有动作没进展**：工具在跑、步数在涨，但没有产生任何新证据（没写成东西、没跑通命令、
  没读到新文件、结果和上一次一字不差）。这时候继续往下走，只是把同一个错误假设走得更远。

两种都不是"工具失败"，所以现有的失败处理一条都不会触发。这里补上这一层：拦住、说清楚
已经重复了几次 / 空转了几步，并要求换思路或重新规划。

设计取舍
--------
* **按轮计数，不跨轮**：一轮结束状态就丢。跨轮重复往往是用户自己让"再试一次"。
* **豁免轮询类和记账类工具**：`bash_output` 就是要拿同样的 `bash_id` 反复问；
  `todo_write`/`progress_update` 这类记账调用本来就该重复出现。把它们算进去等于自找误报。
* **线程安全**：只读工具会被 `ThreadPoolExecutor` 并行派发，计数必须带锁。
* **拦截而不是终止**：返回一条 `ToolResult(False, ...)` 走现有的"已拦截"通道，
  模型看得到、能改做法；绝不直接结束这一轮。
"""
from __future__ import annotations

import hashlib
import json
import threading

#: 同一 (工具, 参数) 在一轮里最多允许出现几次；第 N+1 次拦截。
DEFAULT_REPEAT_LIMIT = 3
#: 连续多少个实质步骤没有产生新证据就判定空转。
DEFAULT_STALL_LIMIT = 8

#: 轮询类：同参数反复调用是**正确用法**，不参与重复计数。
_POLLING_TOOLS = frozenset({"bash_output"})
#: 记账类：与 progress_reporting.META_TOOLS 同义，独立写一份避免反向依赖。
_META_TOOLS = frozenset({
    "progress_update", "todo_write", "self_critique",
    "task_read", "task_step", "task_log", "task_resume",
})
_EXEMPT = _POLLING_TOOLS | _META_TOOLS

#: 结果指纹只取前这么多字符。工具结果动辄几千字，全量哈希既慢又对"尾部有个时间戳"
#: 这种伪差异过敏。
_RESULT_FINGERPRINT_CHARS = 600


def _fingerprint(name: str, args) -> str:
    try:
        payload = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        payload = repr(args)
    return hashlib.sha1(f"{name}\x00{payload}".encode("utf-8", "replace")).hexdigest()


class LoopGuard:
    """一轮的打转状态。`agent_loop` 每轮新建一个。"""

    def __init__(self, repeat_limit: int = DEFAULT_REPEAT_LIMIT,
                 stall_limit: int = DEFAULT_STALL_LIMIT) -> None:
        self.repeat_limit = max(2, int(repeat_limit))
        self.stall_limit = max(3, int(stall_limit))
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}          # 调用指纹 → 出现次数
        self._results: set[str] = set()           # 见过的 (工具, 结果) 指纹
        self._blocked: set[str] = set()           # 已经因重复拦过的指纹（同一句话不重复说）
        self.steps_since_progress = 0
        self.stall_notices = 0                    # 已经因空转提醒过几次

    # ── 前置检查 ────────────────────────────────────────────────────────────
    def check(self, name: str, args) -> str | None:
        """要拦就返回拦截文案，放行返回 None。在工具真正执行**之前**调用。"""
        if not name or name in _EXEMPT:
            return None
        key = _fingerprint(name, args)
        with self._lock:
            count = self._calls.get(key, 0)
            if count < self.repeat_limit:
                return None
            first_time = key not in self._blocked
            self._blocked.add(key)
        if not first_time:
            return (f"已拦截：`{name}` 用完全相同的参数仍在重复调用。"
                    "这条路已经证明走不通，必须换做法或向用户澄清，不要再试同一个调用。")
        return (f"已拦截重复调用：`{name}` 已经用**完全相同的参数**调用了 {self.repeat_limit} 次，"
                "结果不会变。请停下来重列假设：换参数、换定位思路、换工具，"
                "或者直接告诉用户你卡在哪、需要什么信息。")

    def stall_feedback(self) -> str | None:
        """空转到阈值时返回一段重规划提示；未到阈值返回 None。"""
        with self._lock:
            if self.steps_since_progress < self.stall_limit:
                return None
            self.steps_since_progress = 0
            self.stall_notices += 1
            steps = self.stall_limit
        return (f"已拦截：连续 {steps} 步没有产生任何新证据"
                "（没有成功的写入、没有跑通的命令、没有读到新内容）。"
                "继续按当前思路走下去只会把同一个假设走得更远。"
                "请先用 todo_write 修订计划：写下你已经排除了什么、当前最可能的解释是什么、"
                "下一步准备用什么证据验证它；确实缺信息就停下来问用户。")

    # ── 观察结果 ────────────────────────────────────────────────────────────
    def observe(self, name: str, args, ok: bool, text: str) -> None:
        """记一次工具结果。在工具执行**之后**调用。"""
        if not name:
            return
        key = _fingerprint(name, args)
        result_key = _fingerprint(name, (text or "")[:_RESULT_FINGERPRINT_CHARS])
        with self._lock:
            if name not in _EXEMPT:
                self._calls[key] = self._calls.get(key, 0) + 1
            if name in _META_TOOLS:
                return          # 记账调用既不算进展也不算空转
            # 进展 = 这个工具吐出了**以前没见过的**内容。成功但结果一字不差
            # （同一份报错、同一份空列表）不算进展。
            if ok and result_key not in self._results:
                self._results.add(result_key)
                self.steps_since_progress = 0
            else:
                self.steps_since_progress += 1
