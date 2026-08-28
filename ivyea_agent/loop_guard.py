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
#: 连续多少次调用**全是记账**就叫停。取 stall 的一半：记账本来就该穿插在干活之间，
#: 连着这么多次一件实事没做，说明它在原地整理待办而不是在推进。
DEFAULT_BOOKKEEPING_LIMIT = 4

#: 轮询类：同参数反复调用是**正确用法**（等后台任务出新输出），两条计数都不参与。
_POLLING_TOOLS = frozenset({"bash_output"})
#: 记账类：与 progress_reporting.META_TOOLS 同义，独立写一份避免反向依赖。
_META_TOOLS = frozenset({
    "progress_update", "todo_write", "self_critique",
    "task_read", "task_step", "task_log", "task_resume",
})
#: 按**参数指纹**计重复时豁免的工具。记账类在这里豁免 —— 它们本来就该反复出现。
_EXEMPT = _POLLING_TOOLS | _META_TOOLS

#: 结果看起来是"这次没成"的特征。工具层的拒绝大多 ok=True（它成功地告诉你不行），
#: 所以不能只看 ok。
_REJECTION_MARKS = ("⚠", "已拦截", "已拒绝")

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
                 stall_limit: int = DEFAULT_STALL_LIMIT,
                 bookkeeping_limit: int = DEFAULT_BOOKKEEPING_LIMIT) -> None:
        self.repeat_limit = max(2, int(repeat_limit))
        self.stall_limit = max(3, int(stall_limit))
        self.bookkeeping_limit = max(2, int(bookkeeping_limit))
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}          # 调用指纹 → 出现次数
        self._results: set[str] = set()           # 见过的 (工具, 结果) 指纹
        self._blocked: set[str] = set()           # 已经因重复拦过的指纹（同一句话不重复说）
        # 「同一个工具连续拿回同一句拒绝」的连击数。**这条比参数指纹更要紧**：
        # 实测里模型连发 90 次 progress_update，每次把 summary 的措辞改一点点，
        # 于是参数指纹永远对不上，而拿回来的拒绝一字不差。真正说明"卡住了"的是结果，
        # 不是参数。
        self._last_rejection: dict[str, str] = {}
        self._rejection_streak: dict[str, int] = {}
        # 连续多少步只在记账、一次实质工作都没有。
        #
        # 这是复核时打出来的空档：**成功的**记账风暴此前谁也拦不住 —— todo_write 每次
        # 参数都不同（参数指纹对不上）、结果都是成功（拒绝连击不触发）、又是记账类
        # （不计空转）。实测 max_steps=5 的一轮跑满了 15 个模型步，全在写 todo。
        self._bookkeeping_streak = 0
        # 「同一个工具连续碰壁」的连击数，**不要求拒绝一字不差**。实测里模型会在
        # 三四句不同的拒绝之间轮着撞，每句都不连续重复，于是上面那条也接不住。
        # 阈值放宽到两倍：轮着换做法本身是合理的，一直换不出去才是卡死。
        self._any_rejection_streak: dict[str, int] = {}
        self.steps_since_progress = 0
        self.stall_notices = 0                    # 已经因空转提醒过几次

    # ── 前置检查 ────────────────────────────────────────────────────────────
    def check(self, name: str, args) -> str | None:
        """要拦就返回拦截文案，放行返回 None。在工具真正执行**之前**调用。"""
        if not name or name in _POLLING_TOOLS:
            return None
        with self._lock:
            streak = self._rejection_streak.get(name, 0)
        if streak >= self.repeat_limit:
            return (f"已拦截：`{name}` 已经连续 {streak} 次拿回**完全相同的拒绝**，"
                    "改的只是措辞、不是做法。请照上一条结果指出的问题真正换一步做；"
                    "如果那一步做不到，就停下来告诉用户你卡在哪 —— 再发一遍结果不会变。")
        with self._lock:
            any_streak = self._any_rejection_streak.get(name, 0)
        if any_streak >= self.repeat_limit * 2:
            return (f"已拦截：`{name}` 已经连续 {any_streak} 次被拒绝，一次都没成功过。"
                    "这说明你和这个工具的前置条件之间有个死结，继续换措辞试探只会把这一轮耗光。"
                    "请换一条路（改用别的工具、把这一步标成 blocked/skipped 并说明原因），"
                    "或者停下来把你卡在哪告诉用户。")
        if name in _EXEMPT:
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

    def bookkeeping_feedback(self) -> str | None:
        """连着只在记账、一件实事没做时的提示。未到阈值返回 None。"""
        with self._lock:
            if self._bookkeeping_streak < self.bookkeeping_limit:
                return None
            n = self._bookkeeping_streak
            self._bookkeeping_streak = 0
        return (f"已拦截：连续 {n} 次调用全是记账（todo_write / progress_update 这类），"
                "一件实事都没做。计划已经列清楚了就去执行 —— 读文件、跑命令、查数据，"
                "随便哪一步都行；如果是卡在汇报流程本身过不去，直接把卡点告诉用户，"
                "不要继续在待办列表上来回改。")

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

    # ── 局面变了 ────────────────────────────────────────────────────────────
    def note_new_instruction(self) -> None:
        """用户在这一轮中途追加了新指令 —— 之前攒下的"卡住"判定全部作废。

        不清的话会出现一种很蠢的失败：模型按老目标撞了几次墙、守卫记下了连击，
        这时用户说"别弄那个了，改成 X"，模型转头去做 X，却因为上一段的连击被
        当场拦下并被要求"停下来告诉用户你卡在哪"。用户刚说完话就被告知卡住了。
        """
        with self._lock:
            self._calls.clear()
            self._blocked.clear()
            self._last_rejection.clear()
            self._rejection_streak.clear()
            self._any_rejection_streak.clear()
            self._bookkeeping_streak = 0
            self.steps_since_progress = 0
            self.stall_notices = 0

    # ── 观察结果 ────────────────────────────────────────────────────────────
    def observe(self, name: str, args, ok: bool, text: str) -> None:
        """记一次工具结果。在工具执行**之后**调用。"""
        if not name:
            return
        key = _fingerprint(name, args)
        result_key = _fingerprint(name, (text or "")[:_RESULT_FINGERPRINT_CHARS])
        body = (text or "").lstrip()
        rejected = (not ok) or body.startswith(_REJECTION_MARKS)
        with self._lock:
            if name in _POLLING_TOOLS:
                return
            # 拒绝连击：同一个工具连续拿回同一句拒绝才算，中间成功一次就清零。
            if rejected:
                self._any_rejection_streak[name] = self._any_rejection_streak.get(name, 0) + 1
                if self._last_rejection.get(name) == result_key:
                    self._rejection_streak[name] = self._rejection_streak.get(name, 0) + 1
                else:
                    self._last_rejection[name] = result_key
                    self._rejection_streak[name] = 1
            else:
                self._last_rejection.pop(name, None)
                self._rejection_streak.pop(name, None)
                self._any_rejection_streak.pop(name, None)
            if name not in _EXEMPT:
                self._calls[key] = self._calls.get(key, 0) + 1
            if name in _META_TOOLS:
                self._bookkeeping_streak += 1
                return          # 记账调用既不算进展也不算空转
            self._bookkeeping_streak = 0
            # 进展 = 这个工具吐出了**以前没见过的**内容。成功但结果一字不差
            # （同一份报错、同一份空列表）不算进展。
            if ok and result_key not in self._results:
                self._results.add(result_key)
                self.steps_since_progress = 0
            else:
                self.steps_since_progress += 1
