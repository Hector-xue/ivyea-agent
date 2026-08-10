"""公开投影：注回给模型的门禁提示不能出现在给人看的记录里。

这条契约破过一次 —— IvyeaOps 任务台把 `[知识引用门禁]` 当成用户发的消息画在了
右侧绿气泡里，同一轮的三版草稿并排摆成三条回答。根因是"哪些 user 消息是注入的"
这份清单在展示端各写一份、新门禁上线时没人同步。所以这里钉两件事：
① 每个门禁的真实产物都能被 is_injected_user_message 认出来（不是拿字面量对拍）；
② 投影会连带摘掉被打回的那版草稿，且不碰工具调用的配对。
"""
from __future__ import annotations

import pytest

from ivyea_agent import transcript


def _citation():
    return {"key": "K1", "id": "x", "title": "t",
            "url": "https://sell.amazon.com/", "authority_tier": "primary", "freshness": "current"}


# ── ① 各门禁的真实产物都在登记表里 ─────────────────────────────────────────

def test_citation_gate_output_is_recognized():
    from ivyea_agent import agent_loop
    from ivyea_agent.agent_tools import ToolContext

    ctx = ToolContext(knowledge_citations=[_citation()])
    status = agent_loop.TurnStatus(max_steps=8)
    fb = agent_loop._citation_gate_feedback(ctx, "没有任何引用的答案", status, lambda _: None)
    assert fb and transcript.is_injected_user_message(fb)


def test_completion_gate_output_is_recognized():
    from ivyea_agent import agent_loop
    from ivyea_agent.agent_tools import ToolContext

    status = agent_loop.TurnStatus(max_steps=8, behavioral_task=True)
    status.wrote_code = True
    fb = agent_loop._verify_gate_feedback(ToolContext(), status, lambda _: None)
    assert fb and transcript.is_injected_user_message(fb)


def test_progress_gate_output_is_recognized():
    from ivyea_agent import progress_reporting
    from ivyea_agent.agent_tools import ToolContext

    ctx = ToolContext(progress_required=True, progress_execution_expected=True)
    fb = progress_reporting.completion_feedback(ctx)
    assert fb and transcript.is_injected_user_message(fb)


def test_verify_gate_feedback_is_recognized():
    """verify 的反馈首行以 ⚠ 打头，不是方括号标记 —— 单独钉一条防它漂走。"""
    from ivyea_agent import verify

    lines = [transcript.gate_text(transcript.VERIFY_GATE, "，请先处理再收尾（未通过不要宣称完成）：")]
    assert transcript.is_injected_user_message("\n".join(lines))
    assert verify._MAX_FOCUSED > 0        # 模块可导入（改过它的 import）


def test_compact_summary_and_ack_are_recognized():
    summary = transcript.gate_text(transcript.COMPACT_SUMMARY, "\n上文讲了广告调价")
    assert transcript.is_injected_user_message(summary)
    kept = transcript.strip_injected([
        {"role": "user", "content": summary},
        {"role": "assistant", "content": transcript.COMPACT_ACK},
        {"role": "user", "content": "继续"},
    ])
    assert [m["role"] for m in kept] == ["user"]
    assert kept[0]["content"] == "继续"


def test_gate_text_rejects_unregistered_marker():
    with pytest.raises(ValueError):
        transcript.gate_text("[新门禁]", " 随便写点什么")


# ── ② 投影行为 ─────────────────────────────────────────────────────────────

def test_strip_drops_gate_message_and_the_draft_it_bounced():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "看一下我的 Listing 状态"},
        {"role": "assistant", "content": "草稿一"},
        {"role": "user", "content": transcript.gate_text(transcript.CITATION_GATE, " 本轮已检索到证据 [K1]")},
        {"role": "assistant", "content": "终稿 [K1]"},
    ]
    kept = transcript.strip_injected(messages)
    assert [m["content"] for m in kept] == ["sys", "看一下我的 Listing 状态", "终稿 [K1]"]


def test_strip_keeps_tool_call_pairing_intact():
    """带 tool_calls 的 assistant 绝不能被当成草稿摘掉 —— 它和 tool 结果是成对的。"""
    messages = [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "grep"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
        {"role": "user", "content": transcript.gate_text(transcript.PROGRESS_GATE, " 先做开始汇报")},
        {"role": "assistant", "content": "汇报完毕"},
    ]
    kept = transcript.strip_injected(messages)
    assert [m["role"] for m in kept] == ["user", "assistant", "tool", "assistant"]
    assert kept[1]["tool_calls"][0]["id"] == "c1"


def test_strip_handles_multimodal_user_content():
    messages = [{"role": "user", "content": [{"type": "text", "text": "这张图"},
                                             {"type": "image_url", "image_url": {"url": "data:x"}}]}]
    assert transcript.strip_injected(messages) == messages


def test_visible_turns_ignores_injected_messages():
    messages = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "草稿"},
        {"role": "user", "content": transcript.gate_text(transcript.CITATION_GATE, " 重写")},
        {"role": "assistant", "content": "终稿"},
        {"role": "user", "content": "第二问"},
    ]
    assert transcript.visible_turns(messages) == 2


def test_public_messages_projection_hides_gate_turn():
    """出口处的集成：任务台恢复历史走的就是这条路。"""
    from ivyea_agent import service

    rows = service._public_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "看一下我的 Listing 状态"},
        {"role": "assistant", "content": "草稿一"},
        {"role": "user", "content": transcript.gate_text(transcript.CITATION_GATE, " 本轮已检索到证据 [K1]")},
        {"role": "assistant", "content": "终稿 [K1]"},
    ])
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert not any("门禁" in r["content"] for r in rows)
