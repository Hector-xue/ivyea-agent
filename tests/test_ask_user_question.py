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
    assert out == {"answers": {"追加的指令什么时候生效？": "真注入"},
                   "auto": True, "reason": "no_channel"}
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
    assert out == {"answers": {"追加的指令什么时候生效？": "排队"}, "auto": False, "reason": ""}


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
