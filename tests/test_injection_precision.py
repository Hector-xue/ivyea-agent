"""注入精度：**不该注进上下文的东西，有没有被注进去**。

为什么单独开一个文件
--------------------
`memory_eval` / `knowledge_quality` 测的都是"该找到的有没有找到"（召回率）。整套检索
从来没有被"不该注的有没有注"考核过 —— 而这正是实际翻车的方式：一句"图片点不开"
（前端 bug）被塞了两份亚马逊 Listing 图片审计手册；一句"版本号写在哪个位置"召回了
DeepSeek harness 的凭据记忆，只因为标题里有"配置""位置"两个通用词。

准确度是**两头**的，所以这里同时钉两件事：

* **误注**（本文件主体）：`reject` 里的东西一条都不许出现在自动注入里；
* **漏检**：该找到的仍要找得到 —— 否则"什么都不注"会拿满分，那是更糟的失败。

用例来源必须是**真实翻车案例**，不是想出来的。下面每一条都在 2026-09-02 的真实
库上复现过，注释里写了当时召回了什么。

这些是**确定性**检查（纯词法/规则，不调模型），所以进默认测试套、每次 CI 都跑。
带 LLM 的判分层在 `answer_evals`，不在这里。
"""
from __future__ import annotations

import pytest


# ── 记忆：自动召回不该捞到的 ────────────────────────────────────────────────
#
# **用例自带数据**，不读开发机上的真实记忆库：CI 上那个库是空的，空库当然不会误注，
# 整批负例会变成假绿 —— 比没有用例更糟。下面这份小库照着真实库的形态造：
# 多数条目都带 "ivyea"（真实库里 86%），标题里有"配置""位置""合并"这类通用词。


@pytest.fixture()
def memlib(ivyea_home):
    """一个复现过真实误召的小型记忆库。"""
    from ivyea_agent import memory_store

    rows = [
        ("reference", "DeepSeek harness 凭据配置位置",
         "本机 dsh harness 的 DeepSeek key 存哪、从哪搬的",
         "harness 的 key 放在 /etc/dsh-web.env，从 ~/.dsh 搬过去的。"),
        ("project", "ivyea-note 服务器环境与进度",
         "Ivyea Note 的服务器环境、构建与进度",
         "ivyea note 部署在 note.ivyea.com，Go 服务端 + SQLite，构建走 CI。"),
        ("project", "ivyea-ops console 双卡合并口径",
         "任务台两张卡合并显示的口径",
         "ivyea ops 的执行过程卡和回答卡合并成一段，按轮次合并。"),
        ("project", "ivyea-agent 发版流程",
         "ivyea agent 的发版步骤",
         "ivyea agent 推 tag 触发 release，版本号写在 __init__。"),
        ("project", "ivyea note 同步方案",
         "Ivyea Note 的多端同步设计",
         "ivyea note 用 OPFS + 服务端同步，冲突按最后写入。"),
        ("domain", "否词护栏",
         "广告否词的判定护栏",
         "≥15 点击 0 单才建议否定，品牌词一律拦下。"),
    ]
    for cat, name, desc, body in rows:
        memory_store.apply("add", name=name, content=body, category=cat,
                           description=desc, source="user", confidence=1.0)
    return memory_store


MEMORY_REJECT = [
    # (查询, 不该出现的记忆名片段, 当时实际召回了什么)
    ("版本号写在哪个位置", "harness",
     "只因为标题里的「位置」两字，召回了 DeepSeek harness 凭据配置位置"),
    ("这个配置放在什么位置", "harness",
     "同上，「配置」「位置」都是万能词"),
    ("我想给 ivyea-agent 加一个导出会话的功能", "note",
     "「ivyea」对上了，召回一堆 ivyea-note 的记忆"),
    ("把之前的改动都推送合并发版吧", "双卡合并",
     "「合并」撞上了完全另一个意思的「console 双卡合并口径」"),
]


@pytest.mark.parametrize("query,forbidden,why", MEMORY_REJECT)
def test_auto_recall_does_not_inject_unrelated_memories(memlib, query, forbidden, why):
    from ivyea_agent import memory

    _, names = memory.auto_recall_text(query, limit=4)
    hit = [n for n in names if forbidden.lower() in n.lower()]
    assert not hit, f"{why}\n  查询：{query}\n  误注：{hit}"


def test_auto_recall_still_finds_what_it_should(memlib):
    """准确度是两头的："什么都不注"不许拿满分。"""
    from ivyea_agent import memory

    _, names = memory.auto_recall_text("harness 的 deepseek key 配在哪", limit=4)
    assert any("harness" in n.lower() for n in names), \
        f"问的就是这条，必须召回 —— 否则误注率漂亮了但东西找不着了。实际：{names}"


# ── 技能：自动注入不该匹配的 ────────────────────────────────────────────────
SKILL_REJECT = [
    ("帮我看下这个图片点不开的问题", ("listing_image", "listing_conversion_audit"),
     "前端 bug 被塞了两份 Listing 图片审计手册，只因为句子里有「图片」"),
    ("这个配置文件怎么改", ("amazon.",), "工程任务不该注入任何亚马逊技能"),
    ("帮我分析一下这段代码", ("amazon.",), "同上，「分析」是通用词不是技能信号"),
]


@pytest.mark.parametrize("query,forbidden,why", SKILL_REJECT)
def test_skill_autoinject_does_not_match_on_generic_words(query, forbidden, why):
    from ivyea_agent import skills

    _, ids = skills.context_for_query(query)
    hit = [i for i in ids if any(f in i for f in forbidden)]
    assert not hit, f"{why}\n  查询：{query}\n  误注：{hit}"


# ── 知识：证据池的噪音 ──────────────────────────────────────────────────────
#
# 这里**故意不测**两件看着像问题、其实是设计的事，写下来免得以后又被人"修掉"：
#
# 1. `user.*` 卡不是噪音。它们的 source_quality 是
#    `account_local_overrides_generic_knowledge` —— 账户本地经验**本来就该**压过
#    通用知识。第一版方案里"把 user 卡降池"是错的，会伤到它们该赢的那些用例。
#    真正的病是同一篇文章被切成多张近似卡**霸榜**，那是下面这条用例管的。
# 2. `governance.professional_knowledge_standard` 出现在证据里也是故意的：
#    `_ensure_evidence_standard` 在"一张权威卡都没命中、手里只有用户长文"时会主动
#    把这张护栏卡挂上 —— 那正是最容易顺着问题编的情形。它出现说明护栏在工作。


def test_same_source_cannot_take_more_than_one_evidence_slot():
    """同一来源在一次召回里最多占一席。

    实测「亚马逊图片怎么优化」的前四条里有三条是同一篇文章的不同切片 ——
    这不是"证据充分"，是把证据位浪费在同一句话上。
    """
    from ivyea_agent import knowledge

    ev = knowledge.evidence_context("亚马逊图片怎么优化", limit=4)
    ids = [str(h.get("id")) for h in (ev.get("hits") or [])]
    # 同一篇上传文档的切片共享前缀（user.knowledge-<日期>-<时刻>），按日期段归组
    groups = [i.rsplit("-", 1)[0] if i.startswith("user.knowledge-") else i for i in ids]
    dup = [g for g in set(groups) if groups.count(g) > 1]
    assert not dup, f"同一来源占了多席：{dup}\n  完整召回：{ids}"


# ── 领域闸：工程任务不该被塞进亚马逊上下文 ──────────────────────────────────
#
# 这道闸 CLI 一直有、**serve 一直没有**，而工作台走的正是 serve。同一句话两条路
# 行为不一样，是这次翻车的直接原因之一。
DOMAIN_GATE = [
    ("帮我看下这个图片点不开的问题", False, "前端 bug"),
    ("这个配置文件怎么改", False, "工程任务"),
    ("帮我分析一下这段代码", False, "工程任务"),
    ("把之前的改动推送合并发版", False, "工程任务"),
    ("我的广告 ACOS 太高", True, "运营问题"),
    ("亚马逊图片怎么优化", True, "运营问题"),
    ("listing 标题写多长", True, "运营问题"),
    ("这个类目还能不能做", True, "口语化的运营问题：没有明显亚马逊词，靠「不像工程任务」兜住"),
]


@pytest.mark.parametrize("query,want,why", DOMAIN_GATE)
def test_serve_domain_gate_matches_cli(query, want, why):
    from ivyea_agent.service import _wants_domain_context

    got = _wants_domain_context(query)
    assert got is want, f"{why}\n  查询：{query}\n  期望{'注入' if want else '不注入'}，实际相反"


def test_serve_and_cli_share_one_domain_judgement():
    """两条路必须用同一套判据，不许各写一套 —— 它们分叉过一次。"""
    from pathlib import Path

    from ivyea_agent import service

    src = Path(service.__file__).read_text(encoding="utf-8")
    assert "_looks_like_code_task" in src and "_is_amazon_domain" in src, \
        "serve 的领域闸必须复用 chat_ui 那两个函数"


# ── 索引层：少而重要的类别不许被条目多的类别挤没 ────────────────────────────
def test_index_layer_reserves_a_slot_for_every_category(ivyea_home):
    """预算按类别顺序先到先得时，排在后面的类别会整类消失。

    实测：133 条的库里 project 占 120 条，吃掉大半预算，排在 CATEGORIES 最后的
    domain（6 条运营打法）**一条都没露出来** —— 而那恰恰是少而重要、最该常驻的一类。
    随着项目记录变多，"可复用的打法"会安静地从模型眼前消失，且没有任何迹象。
    """
    import re

    from ivyea_agent import memory_store

    root = memory_store.mem_dir()

    def write(cat: str, name: str, desc: str) -> None:
        d = root / cat
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{memory_store.slugify(name)}.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\ncategory: {cat}\nkeywords: \n"
            f"created: 2026-09-02\nupdated: 2026-09-02\nsource: user\nconfidence: 1.00\n---\n\n内容\n",
            encoding="utf-8")

    for i in range(120):
        write("project", f"项目记录 {i:03d}", f"第 {i} 项工程进展与约束的完整描述，包含服务名与联调对象")
    for i in range(4):
        write("user", f"偏好 {i}", f"用户长期偏好第 {i} 条")
    for i in range(6):
        write("domain", f"打法 {i}", f"可复用的运营结论第 {i} 条")

    idx = memory_store.index_digest()
    shown = {cat: len(re.findall(rf"^- \[{cat}/", idx, re.M)) for cat in ("user", "project", "domain")}
    assert shown["domain"] >= 4, f"domain 被挤没了：{shown}\n索引层：\n{idx[:400]}"
    assert shown["user"] >= 4, f"user 被挤没了：{shown}"
    assert shown["project"] > 10, f"保底不该把大类饿死：{shown}"
    assert len(idx) <= memory_store.MAX_INDEX_CHARS + 200, "索引层必须有上界"


def test_reflection_reads_the_full_index_not_the_trimmed_one():
    """反思靠目录判断"这条已经有了"。给它摘要版会让它重复建记忆。"""
    from pathlib import Path

    from ivyea_agent import memory_reflect, memory_store

    src = Path(memory_reflect.__file__).read_text(encoding="utf-8")
    assert src.count("REFLECTION_INDEX_CHARS") == 2, "反思的两个调用点都要用全量索引"
    assert memory_store.REFLECTION_INDEX_CHARS > memory_store.MAX_INDEX_CHARS
