"""子 agent 的角色。

在这之前只有一个写死的"只读调研员"：同一段 system prompt、同一套只读工具、同一个步数。
路线图 P7 点名要的「数据分析 / Listing 审核 / 广告执行 / 知识审核分工」一条没落地，
于是查代码和审 Listing 用的是同一句"把交给你的问题查清楚"。

这里把角色做成数据：(system prompt, 工具白名单, 步数)。内置几个常用的，
用户可以在 `~/.ivyea/agents/*.md` 里自己加 —— 对标 Claude Code 的 `.claude/agents/*.md`。

两条硬约束
----------
1. **所有角色都是只读的。** `data_analyst` 看着该给 `run_python`，不给的理由有三条，
   任意一条单独成立就足够：

   * **并行派发时审批会打架。** `dispatch_subagent` 在 `PARALLEL_SAFE` 里，同一步派多个
     子 agent 会跑在 `ThreadPoolExecutor` 的线程里。多个线程同时读 stdin 抢审批输入，
     结果是谁也说不清用户批的是哪一条。
   * **非交互场景根本没有"用户"。** serve、飞书、`chat -p`、定时巡检都没有人在终端前面。
   * **子 agent 的结论主线还要复核。** 让一个"待复核的判断"顺手把事做了，顺序就反了。

   （**注意**：单独派一个子 agent 时 `parallel` 判据不成立，它跑在**主线程**上，
   终端里其实是有人能应答审批的。所以"没人应答"这句话只在上面第一、二条场景成立 ——
   早先这里笼统写成"子 agent 跑在后台线程里"，那是不准确的。）

   要算数就把关键数字带回主线算。

2. **子 agent 永远拿不到 `dispatch_subagent`。** 白名单在 `tools_for()` 里统一剔除，
   不靠各个角色自己记得别写 —— `READONLY_TOOLS = PARALLEL_SAFE | {...}` 这个坑踩过一次了。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

DEFAULT_ROLE = "researcher"
#: 单个子 agent 的步数默认上限（角色可覆盖，仍受 `subagent_max_steps_cap` 封顶）。
DEFAULT_MAX_STEPS = 12


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    system: str
    #: 额外允许的工具名。**只能从 READONLY_TOOLS 里挑** —— 越界的会在 tools_for 里被丢掉。
    #: 空 = 用只读工具全集。
    tools: tuple[str, ...] = ()
    max_steps: int = DEFAULT_MAX_STEPS
    source: str = "builtin"


_COMMON_TAIL = ("\n你不能写文件、不能执行命令、不能改广告，也不要再派子 agent。"
                "拿不到证据时明说「没查到」，**绝不猜**——主线会拿你的结论去做决定。")

BUILTIN_ROLES: dict[str, Role] = {
    "researcher": Role(
        name="researcher",
        description="通用只读调研（默认）",
        system=("你是只读调研子 agent。用只读工具把交给你的问题查清楚，"
                "最后用简洁中文给出结论与依据(文件:行/来源)。" + _COMMON_TAIL),
    ),
    "code_explorer": Role(
        name="code_explorer",
        description="在代码库里定位实现、调用方与影响面",
        system=("你是只读代码勘察子 agent。任务是**定位**：这个功能在哪实现、谁调用它、改它会影响什么。"
                "先 grep/glob/code_search 缩小范围，再 read_file 看真实内容——**不要凭文件名猜**。"
                "结论必须给到 `文件:行`，并说清你**没有**确认到什么。" + _COMMON_TAIL),
        tools=("grep", "glob", "code_search", "code_symbols", "code_impact", "read_file", "list_dir"),
        max_steps=20,
    ),
    "data_analyst": Role(
        name="data_analyst",
        description="拉取并解读账户/广告数据，只读不动手",
        system=("你是只读数据分析子 agent。任务是把数据**取回来并读懂**：样本量够不够、"
                "时间窗合不合适、哪些数字支撑结论、哪些只是噪音。"
                "明确区分「归因销售」和「增量销售」、「账户观测」和「平台规则」。"
                "数据不足以支撑结论时，直接说数据不够以及还缺什么。"
                "**你不能执行代码**，需要计算就把关键数字列出来带回主线。" + _COMMON_TAIL),
        tools=("run_patrol", "run_account_diagnosis", "propose_actions", "read_file",
               "list_dir", "knowledge_search", "memory_search", "memory_read"),
        max_steps=16,
    ),
    "listing_auditor": Role(
        name="listing_auditor",
        description="Listing / 评论 / 图片的只读审核",
        system=("你是只读 Listing 审核子 agent。按标题、五点、图片、评论、评分逐项看，"
                "每条发现都要指到**具体位置和具体证据**，不要给「建议优化标题」这种没有落点的话。"
                "区分「确定的问题」和「值得验证的猜想」。" + _COMMON_TAIL),
        tools=("run_listing_audit", "run_review_audit", "run_offer_audit", "run_image_audit",
               "run_image_ocr", "run_competitor_audit", "knowledge_search", "read_file"),
        max_steps=16,
    ),
    "ads_reviewer": Role(
        name="ads_reviewer",
        description="复核广告动作：数据够不够、护栏有没有越",
        system=("你是只读广告复核子 agent。任务不是提动作，是**挑毛病**：样本量够不够支撑这个判断、"
                "否词会不会误伤品牌词或核心词、调价步长和预算变动有没有越过阈值、这笔钱花错了多久能发现、"
                "能不能回滚。每条都给出你的依据。" + _COMMON_TAIL),
        tools=("run_patrol", "propose_actions", "run_account_diagnosis",
               "knowledge_search", "memory_search", "memory_read", "read_file"),
        max_steps=16,
    ),
    "knowledge_auditor": Role(
        name="knowledge_auditor",
        description="核对事实性结论的来源与适用范围",
        system=("你是只读知识核查子 agent。对给定结论逐条核：这是亚马逊官方事实、账户数据推断，"
                "还是运营经验假设？来源是否真实可查？站点/类目/时间会不会改变结论？"
                "算法未被官方披露的东西不得包装成官方规则。核不实的直接说核不实。" + _COMMON_TAIL),
        tools=("knowledge_search", "skill_search", "skill_view", "web_fetch", "web_search", "web_images",
               "read_file", "memory_search", "memory_read"),
        max_steps=16,
    ),
}


# ── 用户自定义角色：~/.ivyea/agents/*.md ──────────────────────────────────────
def agents_dir() -> Path:
    return config.IVYEA_DIR / "agents"


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


def _parse_agent_file(path: Path) -> Role | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    fm: dict[str, Any] = {}
    body = text
    if m:
        try:
            import yaml
            loaded = yaml.safe_load(m.group(1))
            fm = loaded if isinstance(loaded, dict) else {}
        except Exception:      # noqa: BLE001 —— frontmatter 坏了就当没有，正文照用
            fm = {}
        body = m.group(2)
    name = str(fm.get("name") or path.stem).strip().lower()
    if not _NAME_RE.match(name) or not body.strip():
        return None
    try:
        steps = int(fm.get("max_steps") or DEFAULT_MAX_STEPS)
    except (TypeError, ValueError):
        steps = DEFAULT_MAX_STEPS
    tools = tuple(str(t).strip() for t in (fm.get("tools") or []) if str(t).strip())
    return Role(name=name, description=str(fm.get("description") or "").strip() or f"自定义角色 {name}",
                system=body.strip(), tools=tools, max_steps=max(1, steps), source="user")


def user_roles() -> dict[str, Role]:
    base = agents_dir()
    if not base.is_dir():
        return {}
    out: dict[str, Role] = {}
    for path in sorted(base.glob("*.md")):
        role = _parse_agent_file(path)
        if role:
            out[role.name] = role
    return out


def all_roles() -> dict[str, Role]:
    """内置 + 自定义。**同名时用户的覆盖内置** —— 那是本机作者的明确意图。"""
    roles = dict(BUILTIN_ROLES)
    roles.update(user_roles())
    return roles


def get_role(name: str) -> Role:
    """取角色。名字不认识就退回默认 —— 派个调研员总比整个失败强。"""
    return all_roles().get((name or "").strip().lower()) or BUILTIN_ROLES[DEFAULT_ROLE]


def tools_for(role: Role, readonly_schemas: list) -> list:
    """这个角色能用的工具 schema。

    `readonly_schemas` 已经是只读集（`agent_tools._subagent_schemas()`），所以这里做的是
    **在只读集内部再收窄**：角色声明的工具越界（写工具、不存在的工具）一律丢掉，
    收窄后为空则退回只读全集 —— 一个工具都没有的子 agent 只会白跑一轮。
    """
    allowed = {t["function"]["name"] for t in readonly_schemas}
    wanted = {t for t in role.tools if t in allowed}
    if not wanted:
        return list(readonly_schemas)
    return [t for t in readonly_schemas if t["function"]["name"] in wanted]


def render_list() -> str:
    rows = []
    for name, role in sorted(all_roles().items()):
        mark = "*" if role.source == "user" else " "
        scope = "只读全集" if not role.tools else f"{len(role.tools)} 个工具"
        rows.append(f"{mark} {name:<20} {scope:<10} 步数上限 {role.max_steps:<4} {role.description}")
    tail = f"\n\n（* = 你自己在 {agents_dir()} 定义的；同名会覆盖内置）"
    return "\n".join(rows) + tail
