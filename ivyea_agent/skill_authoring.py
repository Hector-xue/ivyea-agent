"""技能写入校验闸：把"技能该怎么写"从祈祷变成代码。

为什么不是写在 prompt 里
------------------------
hermes 把整套作者规范塞进 `/learn` 的 prompt 里，指望模型照做。那份规范里最要紧的一条
（description 不能超长）它自己在注释里承认是"最常被违反的规则"—— 因为**没有人检查**。
规范只要不可执行，就一定会慢慢烂掉。

所以这里的规则全部可判定、写入时当场拒绝或警告。

规则从哪来
----------
**只立本仓真实存在约束的规则，不照抄别家的数字。**

hermes 要求 description ≤ 60 字符，理由是它的技能索引按 60 字截断、超出部分永远参与不了
路由。ivyea **没有这个截断**（`render_list` 打全文、`_score_parts` 也吃全文），
照搬这个数字就是 cargo cult。这里换成有依据的两条：

* `triggers` 必填 —— `skills._terms()` 不分词，中文查询几乎完全靠 triggers 作为子串命中
  （skills.py 自己的注释就是这么写的）。没有 triggers 的技能，中文用户根本搜不到。
* description 长度只给**宽松上限 + 建议值** —— 它进匹配 haystack，但 `_META_HIT_CAP=3`
  已经封了顶：写成一大段不会更容易命中，只会把信息稀释掉。

错误 vs 警告
------------
* **错误**：会让这个技能**用不了**或**找不到**的问题 —— 直接拒绝写入。
* **警告**：影响可读性/可维护性 —— 照常写入，把话说清楚。
"""
from __future__ import annotations

import re
from typing import Any

#: 技能名（frontmatter 的 name）：小写连字符。id 由它推导（`skills._slug`），
#: 大写和空格会在推导时被吃掉，导致"写进去的名字"和"实际的 id"对不上。
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: description 的硬上限与建议值。硬上限只挡住"把整篇正文塞进描述"这种明显跑偏。
MAX_DESCRIPTION_CHARS = 200
SUGGESTED_DESCRIPTION_CHARS = 80

#: 营销词：出现在描述里等于没说。这些词不改变检索结果，只挤占描述位置。
_MARKETING_WORDS = ("强大", "全面", "无缝", "先进", "健壮", "一站式", "极致", "完美",
                    "powerful", "comprehensive", "seamless", "advanced", "robust")

#: 建议的小节顺序。缺了不拦，顺序不对也不拦 —— 但会说。
SUGGESTED_SECTIONS = ("何时使用", "前置条件", "怎么跑", "速查", "步骤", "坑", "验证")
#: 中英两套都认（外部技能库多是英文写的）。
_SECTION_ALIASES = {
    "何时使用": ("何时使用", "什么时候用", "when to use", "使用场景"),
    "前置条件": ("前置条件", "准备", "prerequisites", "依赖"),
    "怎么跑": ("怎么跑", "如何运行", "how to run", "用法", "usage"),
    "速查": ("速查", "quick reference", "命令速查", "cheatsheet"),
    "步骤": ("步骤", "流程", "procedure", "workflow", "做法"),
    "坑": ("坑", "注意", "pitfalls", "已知问题", "guardrails", "护栏"),
    "验证": ("验证", "verification", "怎么确认", "自检"),
}

#: 触发词长度上限，中英分开算。中文一个字是一个词，8 个字已经是半句话；
#: 英文一个词就要好几个字符，同一个数字卡下去会把 `budget-pacing` 这种正常触发词误伤。
_MAX_TRIGGER_CJK = 8
_MAX_TRIGGER_ASCII = 24
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _trigger_too_long(text: str) -> bool:
    cjk = len(_CJK_RE.findall(text))
    return len(text) > (_MAX_TRIGGER_CJK if cjk >= max(2, len(text) // 2) else _MAX_TRIGGER_ASCII)


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
_BACKTICK_RE = re.compile(r"`([a-z_][a-z0-9_]{2,})`")


def _sections(body: str) -> list[str]:
    return [m.group(1).strip().lower() for m in _HEADING_RE.finditer(body or "")]


def _has_section(headings: list[str], key: str) -> bool:
    return any(any(alias in h for alias in _SECTION_ALIASES.get(key, (key,))) for h in headings)


def _known_tool_names() -> set[str]:
    """agent 真实存在的工具名。用来判"正文里 `xxx` 是不是编的"。"""
    try:
        from .agent_tools import TOOL_SCHEMAS
        return {t["function"]["name"] for t in TOOL_SCHEMAS if isinstance(t, dict)}
    except Exception:      # noqa: BLE001 —— 拿不到就不做这项检查，绝不因此拒绝写入
        return set()


#: 正文里出现的、长得像工具名但其实不是的词。只对**看起来像我们工具**的词报警：
#: 前缀命中说明作者以为有这么个工具，而不是随手写了个变量名。
_TOOL_LIKE_PREFIXES = ("skill_", "memory_", "core_memory_", "run_", "code_", "mcp_",
                       "task_", "web_", "ivyea_ops_", "progress_", "todo_")


def _invented_tools(body: str, known: set[str]) -> list[str]:
    if not known:
        return []
    out: list[str] = []
    for name in _BACKTICK_RE.findall(body or ""):
        if name in known or name in out:
            continue
        if name.startswith(_TOOL_LIKE_PREFIXES):
            out.append(name)
    return out[:5]


def validate(meta: dict[str, Any], body: str, *, known_knowledge: Any = None) -> dict[str, Any]:
    """校验一份待写入的技能。返回 {ok, errors, warnings}。

    `known_knowledge`：判断 knowledge_ids 是否存在的可调用对象（默认用 knowledge.get_card）。
    传进来是为了让测试不依赖真实知识库。
    """
    meta = meta or {}
    body = body or ""
    errors: list[str] = []
    warnings: list[str] = []

    name = str(meta.get("name") or "").strip()
    if not name:
        errors.append("缺少 name（技能名）。")
    elif not NAME_RE.match(name):
        errors.append(f"name「{name}」不合法：只能小写字母、数字和连字符（如 lingxing-ad-patrol）。"
                      "大写和空格会在推导 id 时被吃掉，导致写进去的名字和实际 id 对不上。")

    desc = str(meta.get("description") or meta.get("description_zh") or "").strip()
    if not desc:
        errors.append("缺少 description：它既进检索的匹配范围，也是 `ivyea skill list` 里唯一"
                      "能让人一眼认出这技能干什么的一行。")
    else:
        if len(desc) > MAX_DESCRIPTION_CHARS:
            errors.append(f"description 有 {len(desc)} 字，超过上限 {MAX_DESCRIPTION_CHARS}。"
                          "描述是一句话，不是摘要 —— 详细内容写进正文。")
        elif len(desc) > SUGGESTED_DESCRIPTION_CHARS:
            warnings.append(f"description 有 {len(desc)} 字，建议压到 {SUGGESTED_DESCRIPTION_CHARS} 字以内："
                            "同一个词在描述里最多只算 3 次分，写长了不会更容易命中，只会稀释。")
        hit = next((w for w in _MARKETING_WORDS if w in desc.lower()), "")
        if hit:
            warnings.append(f"description 里的「{hit}」是营销词，不改变检索结果也不告诉别人这技能做什么。")
        if name and name in desc:
            warnings.append("description 复读了技能名，等于浪费了这一行。写它**做什么**。")

    triggers = [t for t in (meta.get("triggers") or []) if str(t).strip()]
    if not triggers:
        errors.append("缺少 triggers（触发词）。这条不是可选项：技能检索不分词，"
                      "中文查询几乎完全靠触发词作为子串命中 —— 没有触发词的技能，"
                      "中文用户搜不到。给 3-8 个用户真会打出来的短词。")
    else:
        long_ones = [t for t in triggers if _trigger_too_long(str(t))]
        if long_ones:
            warnings.append(f"触发词「{long_ones[0]}」太长了。触发词要短（中文 2-6 字），"
                            "长句子永远不会被当成子串命中 —— 用户不会一字不差地打出整句话。")

    if not body.strip():
        errors.append("正文为空。")
    else:
        headings = _sections(body)
        if not headings:
            warnings.append("正文没有任何小节标题，读起来会很吃力。")
        else:
            missing = [s for s in SUGGESTED_SECTIONS if not _has_section(headings, s)]
            if len(missing) >= 5:
                warnings.append("正文缺少大部分建议小节（" + "、".join(missing[:5]) +
                                "）。至少要有「何时使用」「步骤」「验证」这三节。")
            elif missing:
                warnings.append("正文缺少建议小节：" + "、".join(missing) + "。")
        invented = _invented_tools(body, _known_tool_names())
        if invented:
            warnings.append("正文里提到的这些工具**不存在**：" + "、".join(f"`{n}`" for n in invented) +
                            "。别写没有的工具名 —— 照着做的人会卡住。")

    getter = known_knowledge
    if getter is None:
        try:
            from . import knowledge
            getter = knowledge.get_card
        except Exception:      # noqa: BLE001
            getter = None
    if getter is not None:
        for kid in (meta.get("knowledge_ids") or []):
            try:
                found = getter(str(kid))
            except Exception:  # noqa: BLE001
                found = True   # 查不动就不拦
            if not found:
                errors.append(f"knowledge_ids 里的「{kid}」在知识库里不存在。")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def render_report(result: dict[str, Any]) -> str:
    """把校验结果渲染成给模型/用户看的一段话。"""
    lines: list[str] = []
    for e in result.get("errors") or []:
        lines.append("✗ " + e)
    for w in result.get("warnings") or []:
        lines.append("· " + w)
    return "\n".join(lines)


#: frontmatter 字段顺序。**id 必须写在里面**：加载器优先认 `id`，没有它就从 `name` 推导
#: （`skills._slug`），于是 name=lingxing-ad-patrol 会被推成 id=lingxing.lingxing_ad_patrol
#: —— 写进去的 id 和读回来的 id 对不上，`skill_view`/`archive` 全都按原 id 找不到。
_FM_ORDER = ("id", "name", "description", "version", "domain", "triggers", "tools", "knowledge_ids")


def _yaml_scalar(value: str) -> str:
    """frontmatter 标量。含冒号/引号的一律加引号 —— 中文描述里冒号很常见，
    不加引号 YAML 会把它解析成映射然后整段丢掉（而且是静默的）。"""
    text = str(value)
    if any(ch in text for ch in ':#"\'\n{}[],&*?|>%@`') or text.strip() != text:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'
    return text


def render_frontmatter(meta: dict[str, Any], body: str) -> str:
    """meta + 正文 → 完整的 SKILL.md（YAML frontmatter 格式，ADR-0009 的通行写法）。"""
    lines = ["---"]
    for key in _FM_ORDER:
        value = meta.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value if str(v).strip()]
            if not items:
                continue
            lines.append(f"{key}: [" + ", ".join(_yaml_scalar(i) for i in items) + "]")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    lines.append("")
    lines.append((body or "").strip())
    lines.append("")
    return "\n".join(lines)
