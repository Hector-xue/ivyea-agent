"""目标模式：把一句话变成一份可验收的契约，达成之前不许收尾。

这个模式解决的是一个很具体的失败：**模型说"完成了"，判定权也在模型手里**。
于是"帮我把 X 做好"往往在第一稿就被宣布完成 —— 代码写了但没跑过、跑过但没验目标
场景、验了一半剩下的只字不提。用户只能自己发现、自己回来说"还不行"，一句话的活
被切成七八轮对话。

目标模式把判定权从模型手里拿走，交给运行时：

1. **立契约**（`derive`）—— 收到指令先把它拆成可验收的标准，每条附「怎么验」。
   这一步是模式的地基：标准写不出来，后面的循环就没有终点。
2. **干活** —— 与平时完全一样（计划、执行、自验证、自我批判那几道门禁原样生效）。
3. **验收**（`judge`）—— 模型想收尾时，运行时逐条对照证据判定，
   没达成就把差距注回去接着干（`agent_loop._goal_gate_feedback`）。

判定方向与自我批判（`critique.needs_fix`）**故意相反**
------------------------------------------------------
自查那道门"拿不准一律按通过"，因为它误判的代价是白跑一轮、甚至把对的答案改坏。
这道门反过来：**拿不准按未达成**。目标模式的全部意义就是"别提前说完成"，
这里放水等于把模式关了。代价（多跑一轮）恰恰是用户选这个模式时愿意付的。

无模型 key 时优雅降级，不抛异常 —— 与 `critique` 同一路数。
"""
from __future__ import annotations

import json
import re
from typing import Any

#: 目标模式下追加进 system prompt 的一段。讲的是**循环纪律**，不是又一份工具说明。
GOAL_SYSTEM_NOTE = """
[目标模式] 本轮处于目标模式：用户给的是一个**要达成的目标**，不是一次问答。纪律如下：
1. 先吃透目标：上下文里的 `[目标契约]` 是本轮的验收标准，它由运行时保管，**判定权不在你手上**——你声称完成不作数，逐条验收通过才作数。契约与用户原话冲突时以用户原话为准，并在回答里指出来。
2. 动手前先用 todo_write 把通往目标的路径拆成可验证的小步，然后**直接开干**。汇报按平时的纪律走即可，别把轮次花在记账上——你在这个模式里被考核的是"标准达没达成"，不是"汇报格式对不对"。
3. 每做完一段**必须自己真实测试**：跑测试、跑真实运行路径、复现目标场景、核对真实输出。编译通过/测试通过 ≠ 目标达成。
4. 测出问题就直接修，修完**重测**，循环到过为止。不要把问题写进回答然后收尾——那等于把活退回给用户。
5. 遇到确实做不到的标准（缺权限、缺凭据、依赖外部人工），不要空转：说清卡在哪、需要什么，把它标成阻塞，然后**继续推进其余标准**。
6. 只有全部标准都拿到真实证据后才做最终汇总。汇总里逐条对照验收标准给出证据。
"""

DERIVE_SYSTEM = ("你是严格的需求分析师。把用户的指令拆成**可验收**的完成标准。"
                 "只输出 JSON，不要解释、不要 Markdown 代码块。")

DERIVE_TEMPLATE = """【用户指令】
{query}
{extra}
把它变成一份验收契约，输出 JSON：
{{
  "objective": "一句话说清最终要达成什么（用用户的语言，不要拔高也不要缩水）",
  "criteria": [
    {{"text": "一条可判定的完成标准", "verify": "怎么验证这条已达成（具体到跑什么命令、看什么输出、复现什么场景）"}}
  ],
  "out_of_scope": ["明确不做的事，防止顺手扩大范围"],
  "risks": ["可能让目标达不成的风险或前置依赖"]
}}

写标准的规矩：
- **每条都要能被第三方独立判定**。"代码质量好"不是标准，"pytest 全绿"、"打开页面看到 X"才是。
- 覆盖用户真正要的**结果**，不是过程。用户要"能用"，标准就得包含"真的跑起来并看到预期输出"。
- 涉及界面/输出/行为的目标，必须有一条"在真实运行环境复现目标场景"的标准——编译或测试通过不能替代它。
- 3 到 7 条为宜。拆得太碎会让判定变成走过场，太粗则判不了。
- 不要把用户没提的功能写成标准；有歧义就在 risks 里点明，别自己替他扩需求。"""

JUDGE_SYSTEM = ("你是严格、克制的验收员。只看**证据**判定，不看主张。只输出 JSON，"
                "不要解释、不要 Markdown 代码块。")

JUDGE_TEMPLATE = """【目标】
{objective}

【验收标准】
{criteria}

【本轮真实工具证据】（运行时逐条记录，模型改不了；格式固定为 `工具(目标) → 成功/失败：输出头`）
{evidence}

【失败/异常记录】
{attention}

【模型给出的收尾回答】
{answer}

逐条判定，输出 JSON：
{{
  "criteria": [{{"index": 1, "status": "met|unmet|unverifiable", "reason": "一句话依据", "evidence": "支撑它的那条证据原文片段"}}],
  "achieved": true,
  "note": "一句话总结还差什么"
}}

判定规矩（**这几条是这次判定的全部依据**）：
- `met` 只给**有真实证据支持**的标准：证据里能看到那条命令跑过、那个测试过了、那个场景复现过。
- 回答里声称做了、但证据里找不到对应记录 → `unmet`，reason 写"只有声称、无证据"。
- 证据显示做了一半、或做了但结果不对 → `unmet`，reason 指出差在哪、下一步该干什么。
- `unverifiable` 只留给**这个环境里客观验不了**的标准（需要线上权限、需要人工线下确认）。做得到却没做不算。
- **拿不准一律 `unmet`**。放过一条没做到的，比多跑一轮的代价大得多。
- 但"拿不准"指的是**证据本身不支持**，不是"证据没按我想要的格式写"。证据行由运行时生成，
  格式就是上面那一种、输出会被截断：一条 `run_command(python3 -c '...') → 成功：3` 就足以
  证明"这条命令输出 3"，不要因为它没有附上完整日志、没有单独一行 stdout 就判未达成。
- 同一条标准**连续两轮判成 unmet 而理由都是"格式/记录不全"**，说明是你在要一份运行时给不出的
  东西：请改为按现有证据实质判断。
- `achieved` 仅当所有标准都是 met 或 unverifiable 时为 true。"""


def _loads(raw: str) -> dict[str, Any]:
    """容忍模型给的 JSON 外面裹着代码块或前后废话。解析不出来返回空 dict。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


def _estimated_usage(prompt: str, completion: str) -> dict[str, int]:
    """`complete()` 不回用量，只能按字符估 —— 与 critique 用同一套估法。

    运行时自己发起的调用**必须记账**：漏在成本闸外面，目标模式的自动续跑就没有刹车。
    """
    try:
        from . import context
        return {"prompt_tokens": int(context._est_text(prompt or "")),
                "completion_tokens": int(context._est_text(completion or ""))}
    except Exception:      # noqa: BLE001
        return {}


def fallback_contract(query: str) -> dict[str, Any]:
    """立不出契约时的兜底：把用户原话当成唯一标准。

    宁可要一条粗标准，也不要没有契约 —— 没有契约的"目标模式"就是普通模式，
    而用户明明按下了那个开关。粗标准至少还能逼出一次真实验证。
    """
    clean = " ".join(str(query or "").split())[:200]
    return {
        "objective": clean or "完成用户指令",
        "criteria": [
            {"text": f"用户指令已完整落地：{clean}" if clean else "用户指令已完整落地",
             "verify": "逐条对照用户原话检查，并在真实运行环境复现一次目标场景"},
            {"text": "改动经过真实验证，不是只声称完成",
             "verify": "跑测试或真实运行路径，核对关键输出"},
        ],
        "out_of_scope": [],
        "risks": ["契约由兜底规则生成（未经模型拆解），验收标准可能偏粗"],
        "degraded": True,
    }


def derive(query: str, provider: Any, *, extra: str = "") -> dict[str, Any]:
    """把指令拆成验收契约。返回 {ok, contract, usage, note}。

    provider 为 None、调用失败、JSON 解析失败 —— 一律回落 `fallback_contract`，
    绝不抛异常把这一轮打断。
    """
    clean = str(query or "").strip()
    if provider is None or not clean:
        return {"ok": False, "contract": fallback_contract(clean), "usage": {},
                "note": "未配置模型，目标契约按兜底规则生成。"}
    user = DERIVE_TEMPLATE.format(query=clean, extra=("\n" + extra + "\n") if extra else "\n")
    try:
        raw = provider.complete(DERIVE_SYSTEM, user, json_mode=True, temperature=0.2, timeout=120.0)
    except Exception as exc:      # noqa: BLE001 —— 立契约失败不能打断这一轮（LLMError 也在内）
        return {"ok": False, "contract": fallback_contract(clean), "usage": {},
                "note": f"目标拆解调用失败（{exc}），已按兜底规则立约。"}
    data = _loads(raw)
    criteria = data.get("criteria") or []
    if not criteria:
        return {"ok": False, "contract": fallback_contract(clean),
                "usage": _estimated_usage(user, raw),
                "note": "目标拆解未给出可用标准，已按兜底规则立约。"}
    contract = {
        "objective": str(data.get("objective") or clean),
        "criteria": criteria,
        "out_of_scope": list(data.get("out_of_scope") or []),
        "risks": list(data.get("risks") or []),
    }
    return {"ok": True, "contract": contract, "usage": _estimated_usage(user, raw), "note": ""}


def _render_criteria(goal: dict[str, Any]) -> str:
    rows = []
    for item in (goal.get("criteria") or []):
        line = f"{item.get('index')}. {item.get('text')}"
        if item.get("verify"):
            line += f"（验证方式：{item['verify']}）"
        if item.get("status") in {"met", "unverifiable"}:
            line += f"［上轮已判定 {item['status']}，除非有新证据推翻，维持原判］"
        rows.append(line)
    return "\n".join(rows) or "（无）"


def judge(goal: dict[str, Any], answer: str, *, evidence: list[str] | None = None,
          attention: list[str] | None = None, provider: Any = None) -> dict[str, Any]:
    """逐条验收。返回 {ok, verdict, usage, note}。

    验不了（没 provider / 调用挂了 / JSON 坏了）时 `ok=False` 且 verdict 为空 ——
    调用方据此**放行**：验收员自己都没上班，不能把用户永远关在门里。
    """
    if provider is None or not (goal or {}).get("criteria"):
        return {"ok": False, "verdict": {}, "usage": {}, "note": "没有可判定的验收标准。"}
    user = JUDGE_TEMPLATE.format(
        objective=goal.get("objective") or goal.get("query") or "（未记录）",
        criteria=_render_criteria(goal),
        evidence="\n".join(f"- {e}" for e in (evidence or [])[-25:]) or "（本轮没有成功的工具结果）",
        attention="\n".join(f"- {a}" for a in (attention or [])[-10:]) or "（无）",
        answer=(answer or "").strip()[:6000] or "（模型没有给出正文）",
    )
    try:
        raw = provider.complete(JUDGE_SYSTEM, user, json_mode=True, temperature=0.1, timeout=120.0)
    except Exception as exc:      # noqa: BLE001 —— 验收挂了不能把交付卡死（LLMError 也在内）
        return {"ok": False, "verdict": {}, "usage": {}, "note": f"验收调用失败（{exc}）。"}
    data = _loads(raw)
    usage = _estimated_usage(user, raw)
    if not data.get("criteria"):
        return {"ok": False, "verdict": {}, "usage": usage, "note": "验收返回无法解析。"}
    return {"ok": True, "verdict": data, "usage": usage, "note": ""}


def gate_body(goal: dict[str, Any], verdict: dict[str, Any]) -> str:
    """门禁注回给模型的正文（不含标记前缀，前缀由 transcript.gate_text 拼）。"""
    from . import goal_store
    left = goal_store.unmet(goal)
    lines = [" 目标尚未达成，**这一轮还不能收尾**。逐条判定如下："]
    for item in (goal.get("criteria") or []):
        mark = {"met": "✓ 已达成", "unmet": "✗ 未达成",
                "unverifiable": "◌ 本环境无法验证", "pending": "· 尚未判定"}.get(
                    str(item.get("status")), "· 尚未判定")
        line = f"{item.get('index')}. [{mark}] {item.get('text')}"
        if item.get("reason"):
            line += f" —— {item['reason']}"
        lines.append(line)
    if verdict.get("note"):
        lines.append(f"验收员结论：{verdict['note']}")
    lines.append(
        f"\n现在只做一件事：把上面 {len(left)} 条未达成的标准做到。"
        "先挑一条最阻塞的，改完**立刻用它的验证方式真实验证一遍**，通过了再挑下一条。"
        "不要重写总结、不要罗列计划、不要问我要不要继续 —— 直接调工具干活。"
        "某条确实做不到（缺凭据/缺权限/需线下人工）就说清卡点，然后继续推进其余各条。")
    return "\n".join(lines)
