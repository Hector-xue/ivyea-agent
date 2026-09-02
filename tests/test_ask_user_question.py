"""拿不准就弹选项：通道、超时兜底、以及"自动决定了什么"的记账。

核心契约：**这个工具一定会返回一份答案**。它存在的意义是让一轮任务在分叉处不卡死，
所以三条没人回答的路径（超时 / 没有可弹选项的界面 / 通道自己坏了）都必须收敛到
"按标了推荐的那一项继续"，并且把这件事记进 ctx.auto_decisions —— 收尾说明是
界面自己画的，不能指望模型在总结里顺口提一句。
"""
from __future__ import annotations

import threading
import time

import pytest

from ivyea_agent import agent_tools, ask
from ivyea_agent.agent_tools import ToolContext


def teardown_function():
    ask.reset_for_tests()


QUESTIONS = [{
    "question": "追加的指令什么时候生效？",
    "header": "投递语义",
    "options": [
        {"label": "真注入", "description": "在当前这一轮里立刻生效", "recommended": True},
        {"label": "排队", "description": "本轮结束后再发"},
    ],
}]


# ── 参数归一 ────────────────────────────────────────────────────────────────

def test_normalize_fills_defaults_and_keeps_recommendation():
    out = ask.normalize([{"question": "选哪个", "options": ["A", {"label": "B", "recommended": True}]}])
    assert out[0]["options"][0] == {"label": "A", "description": "", "recommended": False}
    assert out[0]["options"][1]["recommended"] is True
    assert out[0]["multi_select"] is False


def test_normalize_keeps_only_one_recommendation():
    """两个"推荐"等于没推荐，而超时那条路必须有唯一答案。"""
    out = ask.normalize([{"question": "q", "options": [
        {"label": "A", "recommended": True}, {"label": "B", "recommended": True}]}])
    assert [o["recommended"] for o in out[0]["options"]] == [True, False]


@pytest.mark.parametrize("bad", [
    [], "nope", [{"question": "", "options": ["A", "B"]}],
    [{"question": "q", "options": ["只有一个"]}],
])
def test_normalize_rejects_malformed(bad):
    with pytest.raises(ValueError):
        ask.normalize(bad)


# ── 兜底：一定有答案 ────────────────────────────────────────────────────────

def test_no_channel_takes_recommendation_immediately():
    started = time.time()
    out = ask.resolve(ask.normalize(QUESTIONS), None, timeout_s=300)
    assert out == {"answers": {"追加的指令什么时候生效？": "真注入"}, "auto": True,
                   "reason": "no_channel", "auto_filled": ["追加的指令什么时候生效？"]}
    assert time.time() - started < 1     # 无人值守时一秒都不该等


def test_timeout_takes_recommendation():
    out = ask.resolve(ask.normalize(QUESTIONS), lambda _q, _t: None, timeout_s=0.01)
    assert out["auto"] is True and out["reason"] == "timeout"
    assert out["answers"]["追加的指令什么时候生效？"] == "真注入"


def test_broken_channel_takes_recommendation():
    def boom(_q, _t):
        raise RuntimeError("channel down")
    out = ask.resolve(ask.normalize(QUESTIONS), boom, timeout_s=1)
    assert out["auto"] is True and out["reason"] == "error"


def test_recommendation_defaults_to_first_option_when_unmarked():
    qs = ask.normalize([{"question": "q", "options": ["A", "B"]}])
    assert ask.resolve(qs, None, 1)["answers"]["q"] == "A"


def test_human_answer_wins_and_is_not_marked_auto():
    out = ask.resolve(ask.normalize(QUESTIONS),
                      lambda _q, _t: {"answers": {"追加的指令什么时候生效？": "排队"}}, 5)
    assert out == {"answers": {"追加的指令什么时候生效？": "排队"}, "auto": False,
                   "reason": "", "auto_filled": []}


def test_answers_for_questions_we_never_asked_are_dropped():
    out = ask.resolve(ask.normalize(QUESTIONS),
                      lambda _q, _t: {"answers": {"别的问题": "X"}}, 5)
    assert out["auto"] is True                    # 等于没人答 → 按推荐继续


# ── 远程通道（工作台）────────────────────────────────────────────────────────

def test_remote_ask_resolves_when_someone_clicks():
    sent: list = []
    channel = ask.RemoteAsk(lambda ev, data: sent.append((ev, data)), "sess-1")
    box: dict = {}

    def answer_later():
        for _ in range(200):
            ids = ask.pending_questions()
            if ids:
                ask.resolve_question(ids[0], {"追加的指令什么时候生效？": "排队"})
                return
            time.sleep(0.01)

    t = threading.Thread(target=answer_later)
    t.start()
    box["out"] = channel.ask(ask.normalize(QUESTIONS), 5)
    t.join()

    assert box["out"]["answers"]["追加的指令什么时候生效？"] == "排队"
    assert sent[0][0] == "question_request"
    assert sent[0][1]["questions"][0]["options"][0]["recommended"] is True
    assert ask.pending_questions() == []          # 收摊后不留残留


def test_remote_ask_gives_up_when_the_page_is_gone():
    """页面关了就没人能选了 —— 别在服务端干挂五分钟。"""
    gone = threading.Event()
    gone.set()
    channel = ask.RemoteAsk(lambda *_a: None, "sess-1", client_gone=gone)
    started = time.time()
    assert channel.ask(ask.normalize(QUESTIONS), 300) is None
    assert time.time() - started < 3


def test_remote_ask_emits_timeout_event():
    sent: list = []
    channel = ask.RemoteAsk(lambda ev, data: sent.append(ev), "sess-1")
    assert channel.ask(ask.normalize(QUESTIONS), 0.05) is None
    assert sent == ["question_request", "question_timeout"]


def test_resolve_question_rejects_unknown_request():
    assert ask.resolve_question("nope", {"a": "b"}) is False


# ── 工具层：记账与护栏 ──────────────────────────────────────────────────────

def test_tool_records_auto_decision_and_demands_disclosure():
    ctx = ToolContext()          # 没有 ask_fn = 无人值守
    out = agent_tools.dispatch("ask_user_question", {"questions": QUESTIONS}, ctx)
    assert "真注入" in out
    assert "必须明确说明" in out          # 逼模型在总结里交代
    assert ctx.auto_decisions == [{
        "question": "追加的指令什么时候生效？", "header": "投递语义",
        "chosen": "真注入", "reason": "no_channel"}]


def test_tool_stops_asking_after_the_cap():
    ctx = ToolContext()
    ctx.asked_count = ask.MAX_ASKS_PER_TURN
    out = agent_tools.dispatch("ask_user_question", {"questions": QUESTIONS}, ctx)
    assert "不再打扰用户" in out and "真注入" in out
    assert ctx.auto_decisions == []       # 没弹就没有"替他定的"这回事


def test_tool_reports_bad_arguments_instead_of_asking():
    ctx = ToolContext()
    out = agent_tools.dispatch("ask_user_question", {"questions": [{"question": "q"}]}, ctx)
    assert out.startswith("参数不对")
    assert ctx.asked_count == 0


def test_tool_is_not_offered_to_subagents():
    """子 agent 不该替用户做选择题 —— 它连界面都没有。"""
    assert "ask_user_question" not in agent_tools.READONLY_TOOLS
    assert "ask_user_question" not in agent_tools.PARALLEL_SAFE


def test_a_partly_answered_card_still_records_what_was_auto_filled():
    """一次问四问、人只点了一问 —— 剩下三问同样是"替他定的"，必须记账。

    实测栽过：真跑那一轮里模型问了四问，脚本只答了第一问，另外三问被按推荐项
    填掉却没进 auto_decisions —— 收尾说明里一个字都不会提，用户永远不知道。
    """
    from ivyea_agent import agent_tools
    from ivyea_agent.agent_tools import ToolContext

    qs = [
        {"question": "口径按哪个？", "header": "口径",
         "options": [{"label": "环比", "recommended": True}, {"label": "同比"}]},
        {"question": "要不要落档？", "header": "落档",
         "options": [{"label": "记下来", "recommended": True}, {"label": "先不记"}]},
    ]
    ctx = ToolContext()
    # 通道只答第一问
    ctx.ask_fn = lambda _q, _t: {"answers": {"口径按哪个？": "同比"}}
    out = agent_tools.dispatch("ask_user_question", {"questions": qs}, ctx)

    assert "同比" in out                                   # 人选的那一问照人的来
    assert [d["question"] for d in ctx.auto_decisions] == ["要不要落档？"]
    assert ctx.auto_decisions[0]["chosen"] == "记下来"
    assert ctx.auto_decisions[0]["reason"] == "partial"
    assert "自动定的" in out                                # 也要提醒模型在总结里说


# ── 跳过 vs 没人答：答案一样，说法必须不一样 ──────────────────────────────
def test_skip_is_not_reported_as_timeout():
    """人在场按了「跳过」，不能说成"你 5 分钟没有选择" —— 那是冤枉他。

    两者的答案完全一样（都按推荐项），区别只在通道回的是 dict 还是 None。
    """
    from ivyea_agent import ask

    qs = ask.normalize([{"question": "走哪条", "options": [
        {"label": "A", "recommended": True}, {"label": "B"}]}])

    skipped = ask.resolve(qs, lambda q, t: {"answers": {}}, 1.0)
    assert skipped["reason"] == "skipped"
    assert skipped["answers"]["走哪条"] == "A" and skipped["auto"] is True
    assert skipped["auto_filled"] == ["走哪条"]      # 照样记账，收尾要说明

    nobody = ask.resolve(qs, lambda q, t: None, 1.0)
    assert nobody["reason"] == "timeout"
    assert nobody["answers"] == skipped["answers"]   # 答案一样，说法不一样


def test_tool_wording_for_skip_does_not_blame_the_user(tmp_path, monkeypatch):
    from ivyea_agent import agent_tools

    ctx = agent_tools.ToolContext(workspace=str(tmp_path))
    ctx.ask_fn = lambda questions, timeout: {"answers": {}}      # 人在场，全跳过
    out = agent_tools.dispatch("ask_user_question", {"questions": [
        {"question": "走哪条", "options": [{"label": "A", "recommended": True}, {"label": "B"}]}]}, ctx)
    assert "跳过" in out and "没有选择" not in out
    assert ctx.auto_decisions and ctx.auto_decisions[0]["reason"] == "skipped"


# ── 终端：自己写 / 跳过 ────────────────────────────────────────────────────
def _tty(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": staticmethod(lambda: True)})())


def test_terminal_free_text_becomes_the_answer(monkeypatch):
    """给的选项都不对时，用户写的那句话就是答案 —— 后端对值不做校验。"""
    from ivyea_agent import ask, tui

    _tty(monkeypatch)
    monkeypatch.setattr(tui, "select", lambda *a, **k: ask.OTHER_KEY)
    monkeypatch.setattr(tui, "prompt_text", lambda *a, **k: "YAML，我要自己定")
    qs = ask.normalize([{"question": "什么格式", "options": [
        {"label": "MD", "recommended": True}, {"label": "JSON"}]}])
    assert ask.TerminalAsk().ask(qs, 5.0) == {"answers": {"什么格式": "YAML，我要自己定"}}


def test_terminal_free_text_left_empty_counts_as_unanswered(monkeypatch):
    """打开输入又改主意（空串）＝这题没答，交给 resolve 按推荐补。"""
    from ivyea_agent import ask, tui

    _tty(monkeypatch)
    monkeypatch.setattr(tui, "select", lambda *a, **k: ask.OTHER_KEY)
    monkeypatch.setattr(tui, "prompt_text", lambda *a, **k: "")
    qs = ask.normalize([{"question": "什么格式", "options": [
        {"label": "MD", "recommended": True}, {"label": "JSON"}]}])
    assert ask.TerminalAsk().ask(qs, 5.0) == {"answers": {}}


def test_terminal_skip_returns_a_dict_not_none(monkeypatch):
    """跳过必须回**空 dict**，不是 None —— None 会被 resolve 当成"没人答"。"""
    from ivyea_agent import ask, tui

    _tty(monkeypatch)
    monkeypatch.setattr(tui, "select", lambda *a, **k: ask.SKIP_KEY)
    qs = ask.normalize([{"question": "什么格式", "options": [
        {"label": "MD", "recommended": True}, {"label": "JSON"}]}])
    got = ask.TerminalAsk().ask(qs, 5.0)
    assert got == {"answers": {}}
    assert ask.resolve(qs, lambda q, t: got, 1.0)["reason"] == "skipped"


def test_terminal_menu_always_offers_both_ways_out(monkeypatch):
    """菜单末尾固定两项，顺序也钉住：跳过在最后（降级菜单的空输入落到它）。"""
    from ivyea_agent import ask, tui

    _tty(monkeypatch)
    seen: list = []
    monkeypatch.setattr(tui, "select",
                        lambda t, b, options, **k: (seen.append(options), options[0][0])[1])
    qs = ask.normalize([{"question": "什么格式", "options": [
        {"label": "MD", "recommended": True}, {"label": "JSON"}]}])
    ask.TerminalAsk().ask(qs, 5.0)
    keys = [k for k, _ in seen[0]]
    assert keys == ["MD", "JSON", ask.OTHER_KEY, ask.SKIP_KEY]


def test_terminal_ctrl_c_still_means_nobody_answered(monkeypatch):
    """整张卡按 Ctrl-C 掉 ≠ 跳过：那是没人答，reason 要落到 timeout。"""
    from ivyea_agent import ask, tui

    _tty(monkeypatch)
    def _boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(tui, "select", _boom)
    qs = ask.normalize([{"question": "什么格式", "options": [
        {"label": "MD", "recommended": True}, {"label": "JSON"}]}])
    assert ask.TerminalAsk().ask(qs, 5.0) is None


def test_prompt_text_never_blocks_a_pipe(monkeypatch):
    """非 tty 直接返回空串 —— 在管道里等 input() 会把整轮吊死。"""
    import sys

    from ivyea_agent import tui

    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    monkeypatch.setattr(tui, "_ACTIVE_PROMPT", None)
    assert tui.prompt_text("问题", "说明") == ""


# ── 全屏 TUI：自由作答走主输入框（工具线程 marshal 回 app）──────────────────
def test_tui_prompt_is_marshalled_to_the_running_app(monkeypatch):
    """挂了 active prompt 就必须走它，不能自己 input() —— 终端归 app 管，
    工具线程直接 input() 打出来的字会串进画面。"""
    from ivyea_agent import tui

    monkeypatch.setattr(tui, "_ACTIVE_PROMPT", lambda title, body: "  从 app 收到的  ")
    assert tui.prompt_text("问题", "说明") == "从 app 收到的"


def test_chat_tui_prompt_round_trip():
    """`_prompt_text` 阻塞工具线程，主 app 提交那一行把它唤醒。"""
    import threading

    from ivyea_agent.chat_tui import ChatTUI

    app = ChatTUI(status_fn=lambda: "", turn_fn=lambda *a, **k: {"text": ""})
    got: list[str] = []
    worker = threading.Thread(target=lambda: got.append(app._prompt_text("走哪条", "自己写")))
    worker.start()
    for _ in range(200):                       # 等工具线程把提示挂上去
        if app.pending_prompt is not None:
            break
        time.sleep(0.01)
    assert app.pending_prompt == {"title": "走哪条", "body": "自己写"}
    app._answer_prompt("我自己的答案")
    worker.join(timeout=3)
    assert got == ["我自己的答案"] and app.pending_prompt is None


def test_chat_tui_empty_submission_is_a_valid_non_answer():
    """空回车＝这题不答。必须收下并唤醒，不能当成"什么都没发生"卡住工具线程。"""
    import threading

    from ivyea_agent.chat_tui import ChatTUI

    app = ChatTUI(status_fn=lambda: "", turn_fn=lambda *a, **k: {"text": ""})
    got: list[str] = []
    worker = threading.Thread(target=lambda: got.append(app._prompt_text("走哪条")))
    worker.start()
    for _ in range(200):
        if app.pending_prompt is not None:
            break
        time.sleep(0.01)
    app._answer_prompt("")
    worker.join(timeout=3)
    assert got == [""]


def test_chat_tui_free_answer_outranks_queueing_while_running():
    """接线钉死：回车分支里，自由作答必须排在 `self.running` 排队之前。

    自由作答**就发生在**工具跑着的时候。顺序反了，用户的答案会被当成"追加指令"
    排进队列，选项卡那边永远等不到 —— 而且这种错跑一次正常对话根本看不出来。
    """
    from pathlib import Path

    from ivyea_agent import chat_tui

    src = Path(chat_tui.__file__).read_text(encoding="utf-8")
    body = src[src.index('@kb.add("enter")'):]
    assert body.index("self.pending_prompt is not None") < body.index("if self.running:")


def test_chat_tui_registers_and_clears_the_prompt_channel():
    """挂上和摘掉必须成对：交互命令挂起 app 时也要摘，否则 marshal 到已挂起的
    app 就是死锁（选择器那条早就踩过，这里照抄它的成对写法）。"""
    from pathlib import Path

    from ivyea_agent import chat_tui

    # 只数真调用：那段注释里也写着 `set_active_selector(None)`，按整份文本 count
    # 会把它算进去（第一版就是这么写错的）。
    calls = [ln.strip() for ln in Path(chat_tui.__file__).read_text(encoding="utf-8").splitlines()
             if ln.strip().startswith("_tui_mod.set_active_")]
    sel = [c for c in calls if "set_active_selector(" in c]
    prompt = [c for c in calls if "set_active_prompt(" in c]
    assert len(sel) == len(prompt) == 4, (sel, prompt)
    # 挂上两次（启动 + 交互命令跑完装回）、摘掉两次（退出 + 交互命令挂起前）
    assert sum("None" in c for c in prompt) == 2
