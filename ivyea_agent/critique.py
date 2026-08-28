"""通用自我批判层。

把广告域的 LLM 复核（review.py）泛化成任意任务的"收尾前自查一遍"：
给定任务与回答，按 rubric 逐维度找问题（答非所问/把猜测当事实/遗漏/未验证/风险）。
无模型 key 时优雅降级，不抛异常。

两种用法
--------
* **模型自己调**（`self_critique` 工具）：想查就查，查什么由它自己定。
* **运行时门禁**（`agent_loop._critique_gate_feedback`）：改过真代码、或真下过写指令的
  轮次，收尾前**由运行时**跑一次。这一条是刻意的 —— 「该自查的时候自查」交给模型自觉，
  等于没有；而恰恰是动过东西的那些轮次最不能只靠自觉。

rubric 分域
-----------
一份通用 rubric 覆盖不了差异很大的活：代码改动要问"验证过没有"，广告决策要问
"这笔钱花出去的依据是什么"，知识结论要问"这是官方事实还是你的推断"。所以按域分。
判不准就用通用那份 —— 通用 rubric 是兜底，不是缺省的懒惰。
"""
from __future__ import annotations

from typing import Any, Optional

from .providers import LLMError

CRITIQUE_SYSTEM = ("你是严格、克制的复核者。对给定「任务」与「回答」做批判性自查，"
                   "只输出简短 Markdown，不复述原答案，不奉承。")

DEFAULT_RUBRIC = """按以下维度逐条查，发现问题才写、没问题略过：
1. 需求吻合：是否真正满足任务、有没有答非所问或漏掉子需求。
2. 事实可靠：数字/引用/结论有无把猜测当事实（应能标 [证据]/[推断]）。
3. 关键遗漏：漏掉的边界、风险、反例或前提。
4. 验证到位：若涉及代码/操作，是否真正验证过、有无副作用或安全隐患。
每条一句话、可执行。最后一行给结论：**通过** 或 **建议修正**（列 1-3 条最重要的）。"""

CODE_RUBRIC = """这是一次**代码改动**的交付。按以下维度逐条查，发现问题才写、没问题略过：
1. 需求吻合：改的是不是用户要的那处；有没有顺手改了没被要求的东西（超范围）。
2. 真的验证过：是跑过还是只是"看着对"？测试通过 ≠ 目标行为生效；界面/输出类改动必须
   在真实运行路径上复现过目标场景。只声称"应该可以"的一律算未验证。
3. 消费方契约：改的东西有没有别处在用（调用方、序列化格式、配置键、CLI 参数）；
   有没有悄悄改掉默认值 —— 不做任何设置的老用户会不会因此行为变了。
4. 失败路径：异常/空值/并发/权限不足时会怎样；有没有把错误吞掉让问题更难查。
5. 遗留：有没有半截活、TODO、被注释掉的旧逻辑、写死的临时路径或密钥。
每条一句话、可执行、指到具体文件或函数。最后一行给结论：**通过** 或 **建议修正**（列 1-3 条最重要的）。"""

ADS_RUBRIC = """这是一次**广告投放决策**的交付。按以下维度逐条查，发现问题才写、没问题略过：
1. 数据够不够：样本量/时间窗是否支撑这个结论；点击数太少就下结论等于赌博。
2. 归因边界：归因销售不等于增量销售；账户上看到的现象不等于平台算法的规则。
3. 动作与数据绑定：每条动作是否指到具体的词/campaign/ASIN 和具体数字，而不是泛泛建议。
4. 护栏：否词会不会误伤品牌词/核心词；调价步长、预算变动是否越过既定阈值；是否可回滚。
5. 代价：这笔钱花出去最坏结果是什么，判断错了多久能发现。
每条一句话、可执行。最后一行给结论：**通过** 或 **建议修正**（列 1-3 条最重要的）。"""

KNOWLEDGE_RUBRIC = """这是一次**事实性结论**的交付。按以下维度逐条查，发现问题才写、没问题略过：
1. 来源：结论是否由本轮真实检索到的证据支撑；有没有凭记忆作答却说得像有出处。
2. 分层：官方事实 / 账户观测 / 运营经验 三类是否分开表达，没有被混成一句断言。
3. 适用范围：站点、类目、账户状态、时间是否会改变结论；不确定时有没有说明并建议核对后台。
4. 引用：标注的引用键是否都真实存在、且确实支持它所在的那句话。
每条一句话、可执行。最后一行给结论：**通过** 或 **建议修正**（列 1-3 条最重要的）。"""

RUBRICS = {
    "code": CODE_RUBRIC,
    "ads": ADS_RUBRIC,
    "knowledge": KNOWLEDGE_RUBRIC,
    "general": DEFAULT_RUBRIC,
}

#: 结论行里出现它就算"没过"。模型偶尔会写成"建议修正："或"**建议修正**"，用子串匹配。
NEEDS_FIX_MARK = "建议修正"


def pick_rubric(kind: str = "") -> str:
    return RUBRICS.get((kind or "").lower(), DEFAULT_RUBRIC)


def needs_fix(markdown: str) -> bool:
    """复核结论是不是"建议修正"。拿不准（两个词都在/都不在）一律按**通过**处理。

    方向是刻意的：门禁误判成"要修"的代价是白白多跑一轮、甚至逼模型改坏已经对的答案；
    误判成"通过"的代价只是这一次没帮上忙。宁可少拦。
    """
    text = markdown or ""
    if NEEDS_FIX_MARK not in text:
        return False
    tail = "\n".join(line for line in text.strip().splitlines()[-3:])
    return NEEDS_FIX_MARK in tail and "通过" not in tail.replace("不通过", "")


def _estimated_usage(prompt: str, completion: str) -> dict[str, int]:
    """`complete()` 不回用量，只能按字符估。用 context 那套 CJK 校准的估法，别另发明一个。"""
    try:
        from . import context
        return {"prompt_tokens": int(context._est_text(prompt or "")),
                "completion_tokens": int(context._est_text(completion or ""))}
    except Exception:      # noqa: BLE001
        return {}


def critique(task: str, answer: str, provider, rubric: Optional[str] = None,
             kind: str = "") -> dict[str, Any]:
    """返回 {ok, markdown, note, needs_fix}。provider 为 None（未配模型）时优雅降级，不抛异常。"""
    if provider is None:
        return {"ok": False, "markdown": "", "note": "未配置模型，无法自我批判。",
                "needs_fix": False, "usage": {}}
    task = (task or "（未提供任务描述）").strip()
    answer = (answer or "").strip()
    if not answer:
        return {"ok": False, "markdown": "", "note": "没有可复核的回答内容。",
                "needs_fix": False, "usage": {}}
    user = f"【任务】\n{task}\n\n【回答】\n{answer}\n\n{rubric or pick_rubric(kind)}"
    try:
        md = provider.complete(CRITIQUE_SYSTEM, user, json_mode=False, temperature=0.2)
    except LLMError as e:
        return {"ok": False, "markdown": "", "note": f"自我批判调用失败：{e}",
                "needs_fix": False, "usage": {}}
    md = (md or "").strip()
    # `complete()` 的契约只返回文本，拿不到真实用量。给个按字符估的兜底 ——
    # 这一次调用是运行时自己发起的、用户看不见，**漏在成本闸外面比估得不准更糟**。
    return {"ok": True, "markdown": md, "note": "", "needs_fix": needs_fix(md),
            "usage": _estimated_usage(user, md)}
