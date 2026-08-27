"""每轮自动召回：model-driven → runtime-driven。

P0 让模型**知道**记忆里有什么，这一步让它**不必想起来去查**。
下面几条守的是这个转变里最容易悄悄坏掉的性质：不该查的时候别查、
查到的别重复注入、别把知识卡正文漏出去、别把遗忘打分刷坏。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def mem(ivyea_home):
    from ivyea_agent import memory, memory_store
    memory_store.apply("add", name="领星广告方法论", content="规则引擎 + LLM 复核。",
                       category="domain", description="领星广告优化怎么做",
                       keywords="领星,广告,规则引擎")
    memory_store.apply("add", name="发版纪律", content="未经批准绝不 push。",
                       category="feedback", description="发版前必须先问",
                       keywords="发版,push,批准")
    return memory


# ── trivial 门 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "", "   ", "好的", "好", "行", "嗯嗯。", "收到！", "继续", "下一步", "谢谢~",
    "ok", "OK!", "thanks", "done", "/compact", "在吗？",
])
def test_trivial_prompts_skip_recall(mem, text):
    assert mem.is_trivial_prompt(text) is True


@pytest.mark.parametrize("text", [
    "好的方案是什么", "行不行", "note", "继续优化广告结构", "帮我否个词",
    "k8s 怎么配", "开始做 P1 吧，先说计划",
])
def test_real_prompts_are_not_trivial(mem, text):
    """锚定 + 只允许尾随标点：以 trivial 词**开头**的真问题不能被误杀。

    "好的方案是什么" 曾是这类正则最典型的误伤。
    """
    assert mem.is_trivial_prompt(text) is False


# ── 召回内容 ──────────────────────────────────────────────────────────────────
def test_auto_recall_hits_relevant_memory(mem):
    body, names = mem.auto_recall_text("领星广告怎么优化")
    assert "领星广告方法论" in body
    assert "domain/领星广告方法论" in names


def test_auto_recall_excludes_already_injected(mem):
    """去重：召回块跟着 user 消息一起落盘，不去重会在长会话里堆成山，
    而且被删掉的记忆还会留在历史里继续影响模型。"""
    body, names = mem.auto_recall_text("领星广告怎么优化",
                                       exclude={"domain/领星广告方法论"})
    assert "领星广告方法论" not in body


def test_already_recalled_reads_back_from_conversation(mem):
    """去重状态直接从对话里反查 —— 这样 resume、换进程、compact 之后都自动正确。"""
    body, names = mem.auto_recall_text("领星广告怎么优化")
    messages = [{"role": "user", "content": "问题" + mem.recall_block(body)}]
    assert "domain/领星广告方法论" in mem.already_recalled(messages)


def test_recall_block_says_it_is_not_user_input(mem):
    """不写这句，模型会把召回内容当成用户刚说的话，一本正经回应旧话题。"""
    block = mem.recall_block("  · [domain/x] y")
    assert mem.RECALL_MARKER in block
    assert "不是用户本轮输入" in block


def test_auto_recall_returns_empty_when_nothing_matches(mem):
    """毫不相干的问题必须**什么都不注入**。

    这条是自动召回和 recall 工具的分水岭：语义那一路刻意没有相似度地板
    （绝对阈值会把正确匹配静默杀光），所以它对任何查询都能排出"最相似的四条"。
    人主动要求回忆时给个最佳猜测是对的；每轮无条件注入时，那就是每轮往上下文里
    塞四条随机记忆。自动召回因此额外要求**词法重合 > 0**。
    """
    body, names = mem.auto_recall_text("量子色动力学的渐近自由")
    assert (body, names) == ("", [])


def test_recall_tool_still_answers_when_auto_recall_would_not(mem):
    """反过来：工具路径**不该**被这道地板挡住 —— 人开口问了，给最佳猜测才对。"""
    hits = mem.recall_core("量子色动力学的渐近自由", record=False)
    assert hits["curated"], "语义兜底被误伤了：工具路径不该加词法地板"


def test_auto_recall_does_not_pollute_decay_scores(mem, monkeypatch):
    """自动召回每轮都跑；计入"被使用"会把冷门记忆全刷成热门，遗忘机制当场作废。"""
    from ivyea_agent import memory_decay
    calls = []
    monkeypatch.setattr(memory_decay, "record_hits", lambda *a, **k: calls.append(a))
    mem.auto_recall_text("领星广告怎么优化")
    assert calls == []


def test_recall_tool_does_record(mem, monkeypatch):
    """反过来：用户/模型主动发起的一次回忆**应该**算作"这条记忆被用到了"。"""
    from ivyea_agent import memory_decay
    calls = []
    monkeypatch.setattr(memory_decay, "record_hits", lambda *a, **k: calls.append(a))
    mem.recall_core("领星广告怎么优化", record=True)
    assert calls


def test_recall_tool_and_auto_recall_share_one_core(mem):
    """两条召回路径必须走同一个函数。

    各写一份的话早晚漂移，而漂移的那条不会有人发现 ——
    直到某天发现"工具查得到、自动召回查不到"。
    """
    import inspect
    from ivyea_agent import agent_tools
    src = inspect.getsource(agent_tools._t_recall)
    assert "recall_core" in src
    assert "memory_store.search" not in src


def test_auto_recall_never_leaks_knowledge_card_bodies(mem, monkeypatch):
    """知识卡正文必须经 knowledge_search 登记引证键才能露面。

    自动召回连指针都不给 —— 彻底不存在"拿没登记的 [K?] 标注结论"这条路。
    """
    from ivyea_agent import knowledge
    monkeypatch.setattr(knowledge, "search",
                        lambda *a, **k: [{"title": "禁止出现的知识卡", "id": "K9"}])
    body, _ = mem.auto_recall_text("领星广告怎么优化")
    assert "禁止出现的知识卡" not in body
    assert "K9" not in body


def test_dedupe_does_not_backfill_with_weaker_matches(mem):
    """去重只做减法，不做补位。

    实测发现的坑：为了"填满配额"而把候选窗口扩到 limit+len(exclude)，
    第二轮问同一件事时前几条全被去重挡掉，于是拿勉强过了词法地板的边缘条目补位，
    表现为"聊得越久，注进去的记忆越离题"。
    """
    body, names = mem.auto_recall_text("领星广告怎么优化", limit=1)
    assert names == ["domain/领星广告方法论"]
    # 把唯一那条排除掉之后，应该什么都不注 —— 而不是换一条不相干的顶上
    body2, names2 = mem.auto_recall_text("领星广告怎么优化", limit=1,
                                         exclude={"domain/领星广告方法论"})
    assert (body2, names2) == ("", [])
