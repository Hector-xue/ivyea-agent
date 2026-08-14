"""反思 / 巩固：把零散的情景记忆升华成分类记忆。

**这是"慢慢进化成私人助理"的字面实现机制。** 没有反思，记忆只会越攒越多、越查越慢；
有反思，记忆会越用越薄、越用越准——因为
    6月否了"cheap phone case"、7月否了"phone case bulk"、8月否了"wholesale case"
会被合成为
    这个账号对宽泛批发类词一贯保守，建议默认否。

两道闸门，都是刻意设的：

1. **显著性门槛**（`MIN_EPISODES`）：攒够足够多的新经历才值得跑一次 LLM。
   每轮都反思既贵又没有新东西可看。

2. **证据门槛**（`MIN_EVIDENCE`）：一条洞察必须有 ≥2 条情景记忆支撑才准落盘。
   这是综述里 dual-buffer consolidation 的轻量版——防止把用户一次性的口误、
   临时的调侃固化成"你的长期偏好"。综述明确点名过这个风险：
   自我反思会**固化错误信念**（trustworthy reflection 是公开难题）。

写入统一走 `memory_store.apply`，因此自动继承那边的查重与合并优先规则——
反思不会自己另开一套写入路径，也就不会绕过冲突消解。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from . import config, memory, memory_store

# 攒够多少条新情景记忆才值得跑一次反思
MIN_EPISODES = 12
# 一条洞察至少要有几条情景记忆支撑才准落盘
MIN_EVIDENCE = 2
# 单次反思最多读多少条情景记忆（控制 prompt 体积与成本）
MAX_EPISODES = 120
# 单条情景记忆截断长度：反思要的是"发生过什么"，不需要逐字全文
EPISODE_CHARS = 400

_LAST_TS_KEY = "memory_last_reflect_ts"

_SYS = """你是一个记忆巩固器。输入是一段时间内的零散经历（对话片段、决策、巡检记录），
你的任务是从中提炼出**值得长期记住的规律与结论**，写进分类记忆。

只提炼这几类东西：
- 用户反复表现出的偏好、工作习惯、汇报要求（category=user 或 feedback）
- 在做的事情、目标、约束、进展（category=project）
- 亚马逊运营上可复用的打法、账户规律、经过验证的结论（category=domain）
- 外部资源指针：链接、看板、文档位置（category=reference）

绝不提炼：
- 一次性的闲聊、寒暄、临时状态
- 只在当次任务里成立的细节
- 你不确定的推测

**合并优先于新建**：输入里会给你现有记忆的索引目录。如果某条洞察讲的是目录里已有的那件事，
用 operation="update" 更新那一条（content 要写**合并后的完整正文**，不是增量），
不要新建一条内容雷同的。事实被推翻时用 operation="delete"。没有值得沉淀的东西就返回空列表。

每条洞察必须给出 evidence_count：有几条输入经历支撑这个结论。只被提到一次的东西
evidence_count=1，会被丢弃——这是刻意的，防止把偶然的一句话当成长期规律。

只输出 JSON，格式：
{"operations":[{"operation":"add|update|delete","name":"人能叫出来的记忆名","category":"user|feedback|project|reference|domain","description":"一句话描述","keywords":"逗号分隔","content":"记忆正文：要点、关键过程、结论","evidence_count":2}]}
"""


def last_reflect_ts() -> float:
    try:
        return float(config.get_setting(_LAST_TS_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _min_episodes() -> int:
    try:
        return max(1, int(config.get_setting("memory_reflect_min_episodes", MIN_EPISODES)))
    except (TypeError, ValueError):
        return MIN_EPISODES


def pending(limit: int = MAX_EPISODES) -> List[Dict[str, Any]]:
    """自上次反思以来的新情景记忆。"""
    return memory.episodes_since(last_reflect_ts(), limit=limit)


def should_reflect() -> bool:
    """显著性门槛：够不够本。不够就别烧那次 LLM 调用。"""
    if not config.get_setting("memory_auto_reflect", True):
        return False
    return len(pending()) >= _min_episodes()


def _render_episodes(rows: List[Dict[str, Any]]) -> str:
    out = []
    for r in rows:
        stamp = time.strftime("%Y-%m-%d", time.localtime(r.get("ts") or 0))
        text = (r.get("text") or "").strip().replace("\n", " ")
        out.append(f"[{stamp}] {text[:EPISODE_CHARS]}")
    return "\n".join(out)


def _extract_json(raw: str) -> Optional[dict]:
    """模型偶尔会把 JSON 包在 ```json 里或前后带解释。宽松地捞出第一个对象。"""
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None


def reflect(provider, *, force: bool = False, limit: int = MAX_EPISODES) -> Dict[str, Any]:
    """跑一次反思。返回 {"ok", "applied", "skipped", "message"}，不抛异常。

    `force=True` 跳过显著性门槛（手动 `ivyea memory reflect` 用），
    但**证据门槛不能跳过**——那道闸门防的是错误信念固化，不是省钱。
    """
    rows = pending(limit=limit)
    need = _min_episodes()
    if not force and len(rows) < need:
        return {"ok": True, "applied": [], "skipped": [],
                "message": f"新经历不足（{len(rows)}/{need} 条），暂不反思。"}
    if not rows:
        return {"ok": True, "applied": [], "skipped": [], "message": "没有新的经历可供反思。"}

    index = memory_store.index_digest() or "（当前没有任何分类记忆）"
    user = (f"# 现有记忆索引\n{index}\n\n"
            f"# 本次要巩固的经历（{len(rows)} 条，按时间正序）\n{_render_episodes(rows)}")
    try:
        raw = provider.complete(_SYS, user, json_mode=True, temperature=0.2, timeout=120.0)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "applied": [], "skipped": [], "message": f"反思调用失败：{e}"}

    data = _extract_json(raw)
    if not isinstance(data, dict):
        return {"ok": False, "applied": [], "skipped": [], "message": "反思返回的不是可解析的 JSON。"}

    ops = data.get("operations")
    if not isinstance(ops, list):
        ops = []

    applied: List[str] = []
    skipped: List[str] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        name = str(op.get("name") or "").strip()
        action = str(op.get("operation") or "").strip().lower()
        try:
            evidence = int(op.get("evidence_count") or 0)
        except (TypeError, ValueError):
            evidence = 0
        # 证据门槛只拦 add：update/delete 是对已有记忆的修正，本来就有历史依据；
        # 拿证据数去卡它们反而会让过时的记忆改不掉。
        if action == "add" and evidence < MIN_EVIDENCE:
            skipped.append(f"{name}（仅 {evidence} 条证据，未达 {MIN_EVIDENCE} 条门槛）")
            continue
        res = memory_store.apply(
            action, name=name, content=str(op.get("content") or ""),
            category=str(op.get("category") or ""), description=str(op.get("description") or ""),
            keywords=str(op.get("keywords") or ""))
        (applied if res.get("ok") else skipped).append(
            res.get("message", name) if res.get("ok") else f"{name}：{res.get('message', '')}")

    # 时间水位线推进到本批最后一条：即便这次一条都没落盘，也不该下次再嚼同一批经历。
    config.set_setting(_LAST_TS_KEY, float(rows[-1].get("ts") or time.time()))

    if applied:
        msg = f"反思完成：沉淀 {len(applied)} 条记忆。"
    elif skipped:
        msg = f"反思完成：{len(skipped)} 条洞察未达门槛或被合并规则拦下，无新增。"
    else:
        msg = "反思完成：本批经历里没有值得长期沉淀的东西。"
    return {"ok": True, "applied": applied, "skipped": skipped, "message": msg,
            "episodes": len(rows)}


def status() -> Dict[str, Any]:
    last = last_reflect_ts()
    rows = pending()
    return {
        "auto": bool(config.get_setting("memory_auto_reflect", True)),
        "last_reflect": time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "从未",
        "pending_episodes": len(rows),
        "threshold": _min_episodes(),
        "ready": len(rows) >= _min_episodes(),
    }
