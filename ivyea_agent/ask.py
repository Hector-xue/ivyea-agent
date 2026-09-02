"""「拿不准就把选项弹给用户」的问答通道。

── 为什么要有它 ──────────────────────────────────────────────────────────────
系统提示里一直有一条"澄清"纪律：需求有歧义时先反问、别靠假设硬做。但在非交互的
一轮里，"反问"只能表现为**把问题写进回答然后结束这一轮** —— 用户离开五分钟回来，
看到的是一个问句，什么都没做。

真正需要的是 Claude Code / harness 里那种东西：**方案分叉时弹一组选项**，人点一下
这一轮就接着跑；没人点也不能永远挂着 —— 五分钟后按标了推荐的那一项继续，并且
**明确记账**，收尾时告诉用户"这一项是我替你定的"。

── 结构 ──────────────────────────────────────────────────────────────────────
`ask_fn` 是通道，签名 `(questions: list[dict], timeout_s: float) -> dict | None`：
拿到答案就返回 `{问题: 选中项}`，没人回答返回 None。三个实现：

  · `RemoteAsk`   —— serve/工作台：发 SSE 事件、阻塞等 HTTP 回答（照抄
                     service.RemoteApproval 那套 queue + deadline + 客户端断开）；
  · `TerminalAsk` —— CLI/TUI：走 tui.select 菜单；非 tty 直接回 None；
  · 没有通道      —— 无人值守（cron / 飞书 / -p 管道），`resolve` 立刻按推荐项走，
                     一秒都不等：那种场景下等五分钟纯属浪费。

超时/无通道时**永远有一个确定的答案**：标了 recommended 的那一项，没标就第一项。
这是本模块的核心承诺 —— 决不让一轮任务因为没人点而卡死或半途而废。
"""
from __future__ import annotations

import queue
import secrets
import threading
import time
from typing import Any, Callable, Optional

#: 默认等多久（秒）。用户定的规矩：五分钟没人选就按推荐的做。
#: 可用 `ivyea config set ask_timeout_seconds N` 改。
DEFAULT_ASK_TIMEOUT = 300.0

#: 一轮最多问几次。问是好事，问个不停就是把活推回给用户了。
MAX_ASKS_PER_TURN = 3

MAX_QUESTIONS = 4
MAX_OPTIONS = 4
MIN_OPTIONS = 2

AskFn = Callable[[list[dict], float], Optional[dict]]


# ── 参数归一 ────────────────────────────────────────────────────────────────

def normalize(raw: Any) -> list[dict]:
    """把模型给的 questions 参数收成规范形状，顺手做上限裁剪。

    模型给的东西什么形状都可能有（少字段、多字段、options 是纯字符串列表）。
    这里一律收进 `{question, header, multi_select, options:[{label, description,
    recommended}]}`，形状不对就抛 ValueError —— 让它当场看到错误、改一次参数，
    好过把一个半截问题发到用户面前。
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("questions 必须是非空数组")
    out: list[dict] = []
    for item in raw[:MAX_QUESTIONS]:
        if not isinstance(item, dict):
            raise ValueError("每个 question 必须是对象")
        text = str(item.get("question") or "").strip()
        if not text:
            raise ValueError("question 不能为空")
        opts_raw = item.get("options")
        if not isinstance(opts_raw, list) or len(opts_raw) < MIN_OPTIONS:
            raise ValueError(f"「{text}」至少要给 {MIN_OPTIONS} 个 options")
        options: list[dict] = []
        for opt in opts_raw[:MAX_OPTIONS]:
            if isinstance(opt, str):
                opt = {"label": opt}
            if not isinstance(opt, dict):
                raise ValueError("option 必须是对象或字符串")
            label = str(opt.get("label") or "").strip()
            if not label:
                raise ValueError("option.label 不能为空")
            options.append({
                "label": label,
                "description": str(opt.get("description") or "").strip(),
                "recommended": bool(opt.get("recommended")),
            })
        # 推荐项只能有一个：两个"推荐"等于没推荐，而超时那条路必须有唯一答案。
        seen = False
        for opt in options:
            if opt["recommended"] and seen:
                opt["recommended"] = False
            seen = seen or opt["recommended"]
        out.append({
            "question": text,
            "header": str(item.get("header") or "").strip()[:16],
            "multi_select": bool(item.get("multi_select") or item.get("multiSelect")),
            "options": options,
        })
    return out


def recommended_label(question: dict) -> str:
    """这一问的兜底答案：标了推荐的那项，没标就第一项。"""
    options = question.get("options") or []
    for opt in options:
        if opt.get("recommended"):
            return str(opt.get("label") or "")
    return str(options[0].get("label") or "") if options else ""


def recommended_answers(questions: list[dict]) -> dict[str, str]:
    return {q["question"]: recommended_label(q) for q in questions}


# ── 解析一次提问 ────────────────────────────────────────────────────────────

def resolve(questions: list[dict], ask_fn: Optional[AskFn],
            timeout_s: float = DEFAULT_ASK_TIMEOUT) -> dict[str, Any]:
    """问一次，**一定**拿到一份答案。

    返回 `{answers, auto, reason, auto_filled}`：auto=True 表示整份答案都不是人选的；
    `auto_filled` 是**其中哪几问是替用户填的**（按推荐项）。
    reason: ""（全是人选的）/ partial（答了一部分）/ skipped（人在场，按了跳过）/
    no_channel / timeout / error。

    `skipped` 和 `timeout` 的答案完全一样（都按推荐项），但**说法必须不一样**：
    一个是用户主动放权，一个是没人在。前者说成后者就是在对用户撒谎。

    `auto_filled` 必须端出去，调用方才记得了账。只回一个 auto 布尔是不够的：
    一次可以问四问，人只点了第一问就提交（界面会拦，但别的客户端不一定），
    剩下三问照样是"替他定的"—— 不记下来，用户就永远不知道那三项是谁定的。
    """
    everything = [q["question"] for q in questions]
    if ask_fn is None:
        return {"answers": recommended_answers(questions), "auto": True,
                "reason": "no_channel", "auto_filled": everything}
    try:
        got = ask_fn(questions, float(timeout_s))
    except Exception:  # noqa: BLE001 —— 通道坏了不能把整轮拖死，按推荐继续
        return {"answers": recommended_answers(questions), "auto": True,
                "reason": "error", "auto_filled": everything}
    answers = _clean_answers(questions, got)
    if not answers:
        # 一个答案都没有，但**为什么**没有分两种，不能混作一谈：
        #   · 通道返回 None  = 真的没人理（超时 / 页面关了 / 非 tty）
        #   · 通道返回 dict  = 人在场，按了"跳过"，让我们按推荐来
        # 混着报的后果是对用户撒谎 —— 他明明点了跳过，收尾却说"你 5 分钟没有选择"。
        skipped = got is not None
        return {"answers": recommended_answers(questions), "auto": True,
                "reason": "skipped" if skipped else "timeout",
                "auto_filled": everything}
    # 只答了一部分：没答的按推荐补齐，并如实说是哪几问补的。
    filled: list[str] = []
    for q in questions:
        if not answers.get(q["question"]):
            answers[q["question"]] = recommended_label(q)
            filled.append(q["question"])
    return {"answers": answers, "auto": False,
            "reason": "partial" if filled else "", "auto_filled": filled}


def _clean_answers(questions: list[dict], got: Any) -> dict[str, str]:
    """只认这次真发出去的问题；值原样保留（用户可以自己写"其他"）。"""
    if not isinstance(got, dict):
        return {}
    raw = got.get("answers") if isinstance(got.get("answers"), dict) else got
    if not isinstance(raw, dict):
        return {}
    wanted = {q["question"] for q in questions}
    out: dict[str, str] = {}
    for key, val in raw.items():
        if str(key) not in wanted:
            continue
        if isinstance(val, list):
            val = ", ".join(str(v) for v in val if str(v).strip())
        text = str(val or "").strip()
        if text:
            out[str(key)] = text
    return out


# ── 远程通道（serve / 工作台）────────────────────────────────────────────────
#
# 与审批卡同一套机制：轮次线程阻塞在 queue.get 上，答案由 HTTP 端点从另一个线程
# put 进来。三条兜底路径全部收敛到"没人答"（→ 调用方按推荐继续）：超时、客户端
# 断开、进程重启（队列在内存里，重启即失效，轮次本身也没了）。

_PENDING: dict[str, "queue.Queue[dict]"] = {}
_PENDING_LOCK = threading.Lock()


def resolve_question(request_id: str, answers: Any) -> bool:
    """回送一份答案，解开阻塞中的那一步。未知/已过期的 request_id 返回 False。"""
    with _PENDING_LOCK:
        slot = _PENDING.get(str(request_id or ""))
    if slot is None:
        return False
    try:
        slot.put_nowait({"answers": answers if isinstance(answers, dict) else {}})
    except queue.Full:
        return False    # 已经有答案在路上，忽略重复提交
    return True


def pending_questions() -> list[str]:
    with _PENDING_LOCK:
        return list(_PENDING.keys())


class RemoteAsk:
    """AskFn 的远程实现：发 question_request 事件 → 阻塞等答案 → 返回。"""

    def __init__(self, send: Any, session_id: str,
                 client_gone: "threading.Event | None" = None) -> None:
        self._send = send
        self._session_id = session_id
        self._client_gone = client_gone

    def ask(self, questions: list[dict], timeout_s: float) -> Optional[dict]:
        request_id = secrets.token_hex(8)
        slot: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        with _PENDING_LOCK:
            _PENDING[request_id] = slot
        deadline = time.time() + float(timeout_s)
        try:
            self._send("question_request", {
                "request_id": request_id,
                "session_id": self._session_id,
                "questions": questions,
                "timeout_s": float(timeout_s),
                "expires_at": deadline,
            })
            # 分段等待而不是一次 get(timeout=300)：客户端一断开就尽早收摊，
            # 别把这一步在服务端干挂五分钟。
            while True:
                try:
                    return slot.get(timeout=1.0)
                except queue.Empty:
                    if self._client_gone is not None and self._client_gone.is_set():
                        return None     # 页面关了，没人能选了 → 按推荐继续
                    if time.time() >= deadline:
                        self._send("question_timeout", {
                            "request_id": request_id, "session_id": self._session_id})
                        return None
        finally:
            with _PENDING_LOCK:
                _PENDING.pop(request_id, None)


# ── 终端通道（CLI / TUI）────────────────────────────────────────────────────

#: 菜单里那两个不是"选项"的选项。用不可能撞上真 label 的前缀，免得模型某天真写了
#: 一个叫「跳过」的选项就把这条支路劫走。
OTHER_KEY = "\x00ivyea:other"
SKIP_KEY = "\x00ivyea:skip"


class TerminalAsk:
    """AskFn 的终端实现：一问一个菜单（tui.select）。

    菜单末尾固定挂两项，和工作台那张卡对齐：
      · ✎ 自己写一个答案 —— 给的选项都不对时，用户得能说自己的话；
      · ⤼ 跳过这题     —— 明确把这题交回给模型，按推荐项走。

    「跳过」放在最后不是随手排的：`tui.select` 的降级路径（非 tty 的编号菜单）约定
    空输入/无效输入落到最后一项，而"不选＝跳过＝按推荐继续"正好是这里想要的兜底。
    交互菜单那条路回车确认的是高亮项，不受这个约定影响。

    多选在终端上退化成单选 —— 菜单本身只能选一项，与其做一个半吊子的多选界面，
    不如让模型知道终端上只会拿到一项（工具描述里写清楚了）。
    """

    def ask(self, questions: list[dict], timeout_s: float) -> Optional[dict]:
        import sys
        from . import tui
        if not sys.stdin or not sys.stdin.isatty():
            return None            # 非交互：交给 resolve 按推荐走，别在管道上等输入
        answers: dict[str, str] = {}
        for q in questions:
            options = [(opt["label"], _terminal_label(opt)) for opt in q["options"]]
            options.append((OTHER_KEY, "✎ 自己写一个答案…"))
            options.append((SKIP_KEY, "⤼ 跳过这题（按推荐项继续）"))
            title = q["question"]
            # 只说「跳过」，**不要**写成"直接回车＝按推荐项继续"：交互菜单里回车确认的是
            # 当前高亮项，而高亮默认落在第一项上，推荐项未必排第一 —— 那句话会骗人。
            body = "（选「跳过这题」＝按推荐项继续）"
            try:
                picked = tui.select(title, body, options, kind="info")
                if picked == OTHER_KEY:
                    # 空串＝他打开了输入又改主意了，当作这题没答（后面按推荐补齐）。
                    picked = tui.prompt_text(q["question"], "给的选项都不合适？写你自己的答案")
            except (KeyboardInterrupt, EOFError):
                return None        # 整个卡都不要了 → 当作没人答
            if picked and picked != SKIP_KEY:
                answers[q["question"]] = picked
        # 注意这里**一律返回 dict**，哪怕一个都没答：空 dict 是"人在场、他跳过了"，
        # None 才是"没人答"。resolve 靠这个区分，说法完全不同（见它的 docstring）。
        return {"answers": answers}


def _terminal_label(opt: dict) -> str:
    label = str(opt.get("label") or "")
    if opt.get("recommended"):
        label += "（推荐）"
    desc = str(opt.get("description") or "")
    return f"{label} —— {desc}" if desc else label


def reset_for_tests() -> None:
    with _PENDING_LOCK:
        _PENDING.clear()
