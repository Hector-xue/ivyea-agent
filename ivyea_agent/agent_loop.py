"""对话式 Agent 循环（ReAct 工具调用）。

provider.chat(messages, tools) → 若有 tool_calls 则逐个派发(写工具内部走人工
审批)、把结果回灌 → 直到模型给出最终回答。带步数上限防失控。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import (budget as budget_mod, config, context, evidence_ledger, knowledge, loop_guard,
               panels, plan_store, progress_reporting, stream_json, task_scope, thinking, traces,
               transcript, ui)
from .agent_tools import PARALLEL_SAFE, TOOL_SCHEMAS, ToolContext, ToolResult, dispatch_result
from .providers import LLMProvider

SYSTEM_PROMPT = """你是 Ivyea Agent：既是资深亚马逊运营专家，也是合格的编码/工程助手——两类任务都是你的一等本职。按用户当前需求自然切换：运营就按运营流程走，写代码就按工程流程走。
广告：run_patrol(巡检) → propose_actions(看动作) → execute_actions(逐条人工审批执行) → 必要时 rollback。
通用：read_file/list_dir/web_fetch/web_search 读取信息；write_file/edit_file 产出文件；run_python(可用 pandas/openpyxl 读 Excel、算数)、run_command 执行——这些写/执行操作都会弹人工审批。长任务（dev server、watch、长构建等不会很快结束的命令）用 run_command 的 run_in_background=true 后台跑、立即拿 bash_id，再用 bash_output 轮询输出，别在前台干等。
代码：先 grep(内容正则)/glob(按文件名找文件)/code_search(找相关文件)/code_symbols/code_impact 定位，再 read_file 看真实内容（改前必读，别瞎猜路径）。改代码按场景选一个写工具：改单个文件的某一处→edit_file(唯一 old→new)；新建或整体重写文件→write_file；跨多文件/多处关联改动或要顺带跑测试→code_apply_patch(一次提交全部 ops)。每个写工具都是一次调用即审批落盘——**一次逻辑改动只用一个工具，不要先 dry-run 再 execute、也不要同一处既 edit_file 又 code_apply_patch 重复弹审批**。改完测试失败用 run_tests/code_repair 闭环修复。
定位：遇到"某界面/某输出显示不对"，先由可见特征（URL 路径、独有文案、报错串、进程）判断是**哪个程序/代码库在渲染**，再去改——别凭域名或截图来源想当然猜是前端。grep/glob 报"扫描 0 文件/根目录无文件"是**搜索根或 glob 写错**的信号（不是"真没有"），先用 list_dir 核对根目录、修正 path，别换同义关键词反复重搜。定位到关键文件后一次读足，别对同一文件反复分段读。
范围契约：用户消息里出现的 `[任务范围锁定 / 执行契约]` 是运行时根据当前指令、最近上下文和本地仓库生成的硬约束。当前指令明确项目名时优先级最高；截图的浏览器/网页终端外壳不能覆盖该目标。契约标记有歧义时先澄清，不要调用项目搜索或写工具。
委派：需要多角度/独立的调研，可用 dispatch_subagent 派只读子 agent 并行查清，避免主线被探索细节塞满。
MCP：用户接了 MCP 服务器时，用 mcp_list_tools/mcp_list_resources/mcp_list_prompts 发现，mcp_read_resource/mcp_get_prompt 取内容，mcp_call_tool 调用工具（写类会审批）。
技能：上下文里自动注入的技能**只有正文开头一段**，要照着它真正动手前先 skill_view 读全文（带 references/scripts 的还要按需读附属文件），别凭那一小段就开干。走完一套值得复用的流程后，可以用 skill_write 把它沉淀成技能——但只沉淀**下次还会这么干**的通用流程，一次性的具体任务不要建技能。
规划与汇报：多步/复杂任务**动手前先用 todo_write 拆成可验证的小步**，再调用 progress_update(kind=start) 向用户说明目标、范围、阶段、完成标准和第一阶段准备做什么；这两项完成前不要调用实际工作工具。执行时同一时间恰好一个 in_progress。每阶段结束先 progress_update(kind=phase_end) 汇报做了什么、状态、证据、未完成和注意事项，再把 Todo 标 completed/blocked/skipped；下一阶段先更新 Todo，再 progress_update(kind=phase_start) 介绍准备做什么。全部结束后必须 progress_update(kind=final)，汇总已做到、未做到、验证和注意事项，再用一句简短正文收尾。单步、明确的小任务别过度汇报。UI/行为类改动，typecheck/编译/测试通过 ≠ 完成，必须在真实界面或运行环境复现目标场景确认后才算完成。
澄清：当需求**歧义、有多种合理理解、或缺关键输入（ASIN/路径/目标/站点等）**时，先用一两个精准问题反问、停下等用户回答，**别靠假设硬做**；信息足够才进入执行。但简单明确的任务别来回追问。
原则：先拿证据再动手；写操作一律经人工审批，绝不自作主张直接写；动作绑数据、简洁可执行；不要瞎编 ASIN/规格/数字。读文件优先用 read_file 看真实内容，不要假设；**大文件读某几行用 read_file 的 offset/limit，别用 run_command/python 分段读**（那会反复弹审批）。"""

SYSTEM_PROMPT += """
亚马逊知识可靠性：注册/身份验证、上架报错、店铺绩效、政策规则、费用、Listing、FBA、广告等事实性问题，优先使用本轮注入的知识证据；证据不足时调用 knowledge_search。凡采用知识证据支撑结论，必须在对应句末写 [K1] 这类引用键，且只能引用工具或上下文实际提供的键。回答末尾的完整来源清单由系统生成。始终区分“亚马逊官方事实”“账户数据推断”“运营经验/算法假设”；算法未被官方披露时不得包装成官方规则。规则可能因站点、类目、账户和时间不同，适用范围不明时必须说明并建议核对当前站点后台。"""

SYSTEM_PROMPT += """
记忆分层：你有三层记忆，别混用。
- **核心记忆**（core_memory_view / core_memory_edit）：关于用户本人和长期打法的少量事实，**每轮常驻你的上下文**，不需要检索就一直知道。用户表达长期偏好("以后都用中文汇报")、定下长期规则("品牌词永远不否")、纠正你的做法、或透露稳定的身份/目标时，主动写进去——不要只在当轮遵守然后忘掉。写之前先 core_memory_view 看一眼；它有字数上限，写满了要合并旧条目而不是无限追加。
- **分类记忆**（memory_write / memory_search / memory_read）：一事一文件的中期记忆。你的上下文里有一份**记忆索引目录**，列出每条记忆的名字和一句话描述；先看目录判断哪条相关，需要正文时才 memory_read 取——不要一上来就把所有记忆都读一遍。写入时只记**用户的问题/指令要点、关键过程、最终结论**，不要把整段对话抄进去。同一件事有新结论就 update 那一条，别新建第二条；事实被推翻就 delete。
- **情景记忆**（remember / recall）：零散事件、某个 ASIN 的单次结论。跨会话模糊回忆用 recall。
判断标准：**"下次对话我不知道这件事会不会犯错？"** 会且是关于用户/长期规则的 → 核心记忆；会但属于某个具体主题 → 分类记忆；只是一次性事实 → 情景记忆或干脆不记。用户说"记住…"或"更新某某记忆"时，先判断属于哪一层再落盘。"""

PLAN_NOTE = ("\n\n[计划模式] 当前为只读计划模式：可以巡检/分析/提动作，但**不要调用 execute_actions 写入**。"
             "先给出清晰的行动计划，待用户 /approve 批准后再执行。")

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def runtime_context_note(now: datetime | None = None) -> str:
    """实时运行环境提示：把"今天是几号"喂给模型。

    模型本身没有当前时间概念，缺这一行会把"今天/最近/最新"按训练知识里的
    旧日期理解，导致联网搜出过时（如去年）的结果。每轮组装 system prompt 时
    动态生成，确保始终是真实当前日期。
    """
    now = (now or datetime.now()).astimezone()
    stamp = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    weekday = _WEEKDAY_CN[now.weekday()]
    return (
        f"\n\n[运行环境] 当前真实日期时间：{stamp}（{weekday}）。"
        "凡涉及\"今天/现在/最近/最新/本周/今年\"等相对时间，一律以此为准；"
        "不要凭记忆假设年份。web_search/web_fetch 抓到的网页可能是旧闻，"
        "务必对照此日期核对时效，发现结果不在目标时间范围内时，应调整查询重搜或明确告知用户。"
    )

# 单轮工具调用预算。设得高，让 agent 像 Claude Code / Codex 那样一口气把任务做完，
# 而不是动不动撞上限逼用户手动说"继续"。这是防跑飞的安全上限，不是常规停止点；
# 正常任务模型不再调工具时会自然收尾，远early于此。可用 `ivyea config set
# chat_max_tool_steps N` 调整（serve/ops 对话也读同一设置）。
DEFAULT_MAX_TOOL_STEPS = 200
DEFAULT_TOOL_WARNING_REMAINING = 8
CODE_WRITE_TOOLS = {"write_file", "edit_file", "code_apply_patch"}   # 触发完成前自验证门禁的写工具
# 「写了东西」不等于「改了行为」。文档/说明/纯数据文件没有任何可运行的行为可验证，
# 对它们要求"跑一遍真实运行路径"是纯误报——一个被判成 behavioral 的任务（问句里带
# "界面/显示/输出/颜色"等词就会命中），哪怕这一轮只写了一份 .md 报告，也会被逼着
# 去 run_command。行为门禁因此只认真正的代码/配置文件。
_NON_CODE_SUFFIXES = frozenset({
    ".md", ".markdown", ".mdx", ".rst", ".txt", ".text", ".adoc",
    ".csv", ".tsv", ".log", ".patch", ".diff",
})
_VERIFY_CAP = 2                                                       # 门禁最多逼修复几轮，防失控
_NAVIGATION_TOOLS = {"grep", "glob", "code_search", "code_symbols", "code_impact"}
_PROJECT_MUTATION_TOOLS = CODE_WRITE_TOOLS | {"run_command", "run_python", "run_tests"}
_RUNTIME_VALIDATION_TOOLS = {"run_command", "run_python", "bash_output"}
_MAX_NAVIGATION_WITHOUT_READ = 8


def _written_paths(args: dict | None) -> list[str]:
    """从写工具的参数里取出这次落盘的路径。`code_apply_patch` 是一次多文件。"""
    args = args or {}
    paths = [str(args.get("path") or "")]
    for op in args.get("ops") or []:
        if isinstance(op, dict):
            paths.append(str(op.get("path") or ""))
    return [p for p in paths if p.strip()]


def _wrote_code_files(args: dict | None) -> bool:
    """这次写入里有没有真正的代码/配置文件。

    拿不到路径时**按代码算**（保守方向：宁可多验证一次，不可漏过真代码改动）。
    """
    paths = _written_paths(args)
    if not paths:
        return True
    return any(Path(p).suffix.lower() not in _NON_CODE_SUFFIXES for p in paths)


@dataclass
class TurnStatus:
    max_steps: int
    budget: "budget_mod.TurnBudget | None" = None   # 步数/成本预算；None=按 max_steps 裸数（老行为）
    warning_remaining: int = DEFAULT_TOOL_WARNING_REMAINING
    warned: bool = False
    tool_calls: int = 0
    wrote_code: bool = False       # 本轮是否写过文件（决定收尾前是否走自验证门禁）
    wrote_code_files: bool = False # 本轮是否写过**真正的代码/配置**（行为门禁只认它，文档不算）
    verify_rounds: int = 0         # 已触发的自验证逼修复轮数
    behavioral_task: bool = False  # UI/输出/行为任务：测试之外还需要运行路径证据
    runtime_validated: bool = False
    behavior_gate_rounds: int = 0
    citation_gate_rounds: int = 0
    critique_rounds: int = 0       # 收尾自查门禁已经逼修正过几轮（封顶 1）
    self_critiqued: bool = False   # 模型自己调过 self_critique —— 调过就不再由运行时代劳
    compact_warned: bool = False   # 已就"越过压缩阈值但压不动"提醒过一次（每轮至多一次）

    def before_model_step(self, step_idx: int, narrate: Callable[[str], None]) -> None:
        remaining = (self.budget.steps_remaining if self.budget is not None
                     else self.max_steps - step_idx)
        if not self.warned and remaining <= self.warning_remaining:
            self.warned = True
            narrate(ui.message(
                "warn",
                f"本轮工具调用较多（剩余安全预算 {remaining}/{self.max_steps} 步），我会尽快收敛出结果。"
                "若进展不顺，先停下重列假设/换定位思路，别把疑似错误的路径走到底。",
            ))

    def record_tool_call(self, name: str = "") -> None:
        # tool_calls 是**全部**调用数（UI 的时间线序号靠它），预算才区分记不记账。
        self.tool_calls += 1
        if self.budget is not None:
            self.budget.consume(name)

    def observe_tool_result(self, name: str, result: ToolResult, args: dict | None = None) -> None:
        text = result.text or ""
        if name in CODE_WRITE_TOOLS:
            blocked = any(marker in text for marker in (
                "已拦截", "计划模式", "未找到要替换", "写入失败", "编辑失败", "ok: False",
            ))
            if result.ok and not blocked:
                self.wrote_code = True
                if _wrote_code_files(args):
                    self.wrote_code_files = True
                self.runtime_validated = False
                self.behavior_gate_rounds = 0
            return
        if name == "self_critique" and result.ok:
            self.self_critiqued = True
            return
        if not self.wrote_code or name not in _RUNTIME_VALIDATION_TOOLS or not result.ok:
            return
        # 三种真实格式：`[退出码 0]`（run_command/run_python）、`已结束（退出码 0）`
        # （bash_output 收尾）、`returncode=0`（self_manage）。前两种都被"退出码 0"覆盖。
        # 原先这里还有 `已结束（exit=0`，那个字符串本仓一次都没出现过 —— 死判据，删掉。
        if "退出码 0" in text or "returncode=0" in text:
            self.runtime_validated = True


def _resolve_max_steps(value: int | None, setting_key: str) -> int:
    if value is not None:
        return max(1, int(value))
    try:
        return max(1, int(config.get_setting(setting_key, DEFAULT_MAX_TOOL_STEPS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOOL_STEPS


#: 模型步数的硬天花板 = 预算步数 × 这个倍数。
#:
#: 预算只数**干活的**调用（记账调用退款，见 budget.py），所以光靠预算，一个只发
#: progress_update 的死循环可以一直转下去 —— loop_guard 会拦大部分，天花板是最后一道保险。
#:
#: **3 是拍的，不是算出来的。** routing.py 记的那次实测（18 步里 17 步是记账）真按比例算
#: 该取 18 倍，那等于没有天花板；取 1 倍又会把"记账多但确实在干活"的正常长任务掐掉。
#: 3 倍的意思是"允许记账占到三分之二"，这是个判断，不是从数据推出来的 ——
#: 真要调准得先有一批长任务的干活/记账分布，现在没有。
_STEP_CEILING_FACTOR = 3


def _hard_step_ceiling(max_steps: int) -> int:
    return max(1, int(max_steps)) * _STEP_CEILING_FACTOR


def _step_cost(provider, usage: dict, model: str = "") -> float:
    """这一步的估算成本（人民币）。算不出来一律 0 —— 成本闸宁可不拦，也不能因为
    某个模型没在价目表里就把一轮好端端的任务掐了。"""
    try:
        from . import pricing
        return pricing.estimate(model or getattr(provider, "model", "") or "", usage or {})
    except Exception:   # noqa: BLE001
        return 0.0


def _new_loop_guard() -> "loop_guard.LoopGuard":
    """按设置造一轮的打转守卫。两个阈值都可调，设 0 即关掉对应检测。"""
    def _num(key: str, default: int) -> int:
        try:
            return int(config.get_setting(key, default))
        except (TypeError, ValueError):
            return default
    repeat = _num("loop_guard_repeat_limit", loop_guard.DEFAULT_REPEAT_LIMIT)
    stall = _num("loop_guard_stall_limit", loop_guard.DEFAULT_STALL_LIMIT)
    return loop_guard.LoopGuard(
        repeat_limit=repeat if repeat > 0 else 10 ** 6,
        stall_limit=stall if stall > 0 else 10 ** 6,
    )


def _limit_text(max_steps: int, budget: "budget_mod.TurnBudget | None" = None) -> str:
    """到顶了给用户的那句话。**必须说清是撞了哪道闸** —— 步数和成本要采取的动作完全不同：
    前者是"再说一句继续"，后者是"这一轮已经花掉 N 块钱了，你要不要继续花"。"""
    reason = budget.stop_reason() if budget is not None else ""
    if reason == "cost":
        return (f"（本轮已达成本上限：{budget.render()}。任务还没收尾就先停下来了——"
                f"这是刻意的止损点，不是出错。要接着做就说“继续”；"
                f"想放宽用 `ivyea config set chat_max_cost_cny <金额>`，设 0 关掉这道闸。）")
    if reason == "ceiling":
        # 撞天花板 ≠ 预算用完。这里**绝不能**叫用户去调 chat_max_tool_steps ——
        # 预算根本没动，调它一点用没有，那是把人往错方向指。
        return (f"（本轮跑到了模型步数天花板才停：{budget.render()}——"
                f"注意干活的调用只用掉 {budget.steps_used} 次配额，"
                f"绝大多数步数花在了 progress_update/todo_write 这类记账调用上（{budget.steps_refunded} 次）。"
                f"**调高 chat_max_tool_steps 不会有帮助**，瓶颈不在配额。"
                f"多半是某个环节反复卡住导致来回记账，可以说“继续”让它换个思路，"
                f"或者直接告诉它你觉得卡在哪。）")
    return (f"（本轮工具调用已连续执行到安全上限 {max_steps} 步仍未收尾——这通常意味着任务很大或某处卡住了。"
            f"可以直接说“继续”接着做，或用 `ivyea config set chat_max_tool_steps {max_steps * 2}` 进一步提高单轮上限。）")


def _limit_payload(max_steps: int, status: TurnStatus, ctx: ToolContext | None = None) -> str:
    text = (
        f"{_limit_text(max_steps, status.budget)}\n"
        f"本轮已经执行工具调用 {status.tool_calls} 次"
        + (f"（{status.budget.render()}）" if status.budget is not None else "")
        + "。下一轮继续时，请先总结已完成的工具结果，"
        "再从最后一个未完成的小步骤继续；除非必要，不要重复已经成功的工具调用。"
    )
    todos = list(getattr(ctx, "todos", []) or []) if ctx is not None else []
    if todos:
        completed = [str(item.get("content")) for item in todos
                     if isinstance(item, dict) and item.get("status") == "completed"]
        incomplete = [f"{item.get('content')}（{item.get('status', 'pending')}）" for item in todos
                      if isinstance(item, dict) and item.get("status") != "completed"]
        text += "\n已做到：" + ("；".join(completed) if completed else "无")
        text += "\n未做到：" + ("；".join(incomplete) if incomplete else "无")
        attention = list(getattr(ctx, "progress_attention", []) or [])
        text += "\n注意：" + ("；".join(attention[-5:]) if attention else "任务因工具步数上限中断，不能视为全部完成。")
    return text


def _append_limit_context(messages: list, text: str) -> None:
    messages.append({"role": "assistant", "content": text})


def _record_task_interruption(ctx: ToolContext, text: str, status: TurnStatus) -> None:
    task_id = getattr(ctx, "task_id", "")
    if not task_id:
        return
    try:
        from . import task_runner
        task_runner.record_interruption(
            task_id,
            "tool_step_limit",
            text,
            state={
                "session_id": getattr(ctx, "session_id", ""),
                "turn_id": getattr(ctx, "turn_id", ""),
                "max_steps": status.max_steps,
                "tool_calls": status.tool_calls,
            },
        )
    except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError):
        return


def _append_tool_call_msg(messages: list, content, tool_calls: list) -> None:
    """回灌助手的 tool_calls 消息（OpenAI 格式）。"""
    messages.append({
        "role": "assistant", "content": content or None,
        "tool_calls": [{"id": tc["id"], "type": "function",
                        "function": {"name": tc["name"],
                                     "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                       for tc in tool_calls],
    })


def _emit_safe(emit: Callable[[dict], None] | None, ev: dict) -> None:
    """结构化事件回调（stream-json 等）：best-effort，消费端断管/异常不打断主循环。"""
    if emit is None:
        return
    try:
        emit(ev)
    except Exception:  # noqa: BLE001
        pass


def _tool_path(ctx: ToolContext, raw: str) -> Path:
    path = Path(raw or ".").expanduser()
    if not path.is_absolute():
        path = Path(getattr(ctx, "workspace", "") or ".") / path
    return path.resolve()


def _guard_tool_call(ctx: ToolContext, tc: dict,
                     guard: "loop_guard.LoopGuard | None" = None) -> ToolResult | None:
    """Enforce scope/search recovery even when the model ignores prompt instructions."""
    name = tc.get("name") or ""
    if guard is not None:
        repeated = guard.check(name, tc.get("arguments"))
        if repeated:
            return ToolResult(False, repeated)
        if progress_reporting.is_substantive_tool(name):
            stalled = guard.stall_feedback()
            if stalled:
                plan_store.mark_replan(getattr(ctx, "session_id", ""), "连续多步没有新证据，疑似在原地打转")
                return ToolResult(False, stalled)
        else:
            # 记账工具自己也要过一道闸：**成功的**记账风暴不触发重复指纹、
            # 不触发拒绝连击、也不计空转 —— 此前是彻底的空档。
            churn = guard.bookkeeping_feedback()
            if churn:
                plan_store.mark_replan(getattr(ctx, "session_id", ""), "连续多次只在记账，没有推进")
                return ToolResult(False, churn)
    # 计划模式下产出的计划还没被用户批准就跑到执行档来写东西 —— 拦住。
    # 正常对话不会命中：只有真的走过 `/plan` 且没 `/approve` 的会话才有待批准的计划。
    if name in _PROJECT_MUTATION_TOOLS and not getattr(ctx, "plan_mode", False):
        if plan_store.awaiting_approval(getattr(ctx, "session_id", "")):
            return ToolResult(False, "已拦截：当前计划还没有得到用户批准。"
                                     "请把计划完整讲清楚，等用户 /approve 之后再执行写操作。")
    if getattr(ctx, "scope_ambiguous", False) and name in (_NAVIGATION_TOOLS | _PROJECT_MUTATION_TOOLS):
        return ToolResult(False, "已拦截：当前任务同时指向多个项目，目标尚未锁定。请先向用户确认要修改哪个项目。")
    if getattr(ctx, "progress_required", False) and progress_reporting.is_substantive_tool(name):
        if not getattr(ctx, "todos", []):
            return ToolResult(False, "已拦截：复杂/多步任务实际执行前必须先用 todo_write 列出阶段计划。")
        if not getattr(ctx, "progress_started", False):
            return ToolResult(False, "已拦截：请先 progress_update(kind='start')，向用户说明目标、范围、阶段和完成标准。")
        if not int(getattr(ctx, "progress_active_phase", 0) or 0):
            return ToolResult(False, "已拦截：当前没有已汇报开始的阶段。先将下一 Todo 标为 in_progress，再 progress_update(kind='phase_start')。")
    if name in _NAVIGATION_TOOLS and getattr(ctx, "search_recovery_required", False):
        root = getattr(ctx, "workspace", "") or "."
        return ToolResult(False, f"已拦截重复搜索：上一轮扫描进入死胡同。下一步先 list_dir(path={root!r})核对项目根，再继续搜索。")
    if name in _NAVIGATION_TOOLS and int(getattr(ctx, "navigation_since_read", 0)) >= _MAX_NAVIGATION_WITHOUT_READ:
        return ToolResult(
            False,
            f"已拦截继续泛搜：已经导航 {ctx.navigation_since_read} 次仍未读取关键文件。"
            "请 read_file 打开已有结果中的最相关文件；若仍不能确定目标，应停止并向用户澄清。",
        )
    return None


def _observe_tool_discipline(ctx: ToolContext, tc: dict, res: ToolResult) -> None:
    """Update deterministic recovery state and adopt concrete repository evidence."""
    if not res.ok:
        return
    name = tc.get("name") or ""
    args = tc.get("arguments") or {}
    text = res.text or ""
    if name == "read_file" and not any(marker in text for marker in ("不存在", "读取失败", "不是文件")):
        ctx.search_recovery_required = False
        ctx.navigation_since_read = 0
        ctx.consecutive_search_deadends = 0
        raw_path = str(args.get("path") or "").strip()
        adopted = task_scope.adopt_project_from_path(ctx, _tool_path(ctx, raw_path)) if raw_path else ""
        if adopted and f"[范围已锁定] {adopted}" not in text:
            res.text = text + f"\n[范围已锁定] 后续代码搜索根：{adopted}"
        return
    if name == "list_dir" and not any(marker in text for marker in ("不存在", "读取失败", "不是目录")):
        ctx.search_recovery_required = False
        ctx.navigation_since_read += 1
        adopted = task_scope.adopt_project_from_path(ctx, _tool_path(ctx, str(args.get("path") or ".")))
        if adopted and f"[范围已锁定] {adopted}" not in text:
            res.text = text + f"\n[范围已锁定] 后续代码搜索根：{adopted}"
        return
    if name not in _NAVIGATION_TOOLS:
        return
    ctx.navigation_since_read += 1
    if text.lstrip().startswith("⚠"):
        ctx.search_recovery_required = True
        ctx.consecutive_search_deadends += 1
        severity = "已经连续多次定位失败；核对根目录后仍无证据就应向用户澄清。" if ctx.consecutive_search_deadends >= 2 else ""
        res.text = text + "\n[执行护栏] 已暂停后续搜索；下一步必须先 list_dir 核对当前项目根。" + severity
    else:
        ctx.consecutive_search_deadends = 0


def _record_tool_result(ctx: ToolContext, messages: list, tc: dict, res, duration_ms: int,
                        narrate: Callable[[str], None],
                        emit: Callable[[dict], None] | None = None, seq: int = 0,
                        blocked: bool = False,
                        guard: "loop_guard.LoopGuard | None" = None) -> None:
    _observe_tool_discipline(ctx, tc, res)
    # 被护栏拦下的调用**也要记**：反复撞同一道护栏（范围未锁定、先列计划…）同样是
    # 原地打转，而且它既不是"工具失败"也不产生新证据，此前一条守卫都不管它。
    if guard is not None:
        guard.observe(tc.get("name") or "", tc.get("arguments"),
                      res.ok and not blocked, res.text or "")
    if not blocked:
        # 证据台账：跨轮、跨会话地记住"这一轮到底证明了什么"。它自己挑有验证意义的
        # 工具（命令/测试/读写/接口），其余一律跳过。
        try:
            evidence_ledger.record_tool(
                getattr(ctx, "session_id", ""), getattr(ctx, "turn_id", ""),
                tc.get("name") or "", tc.get("arguments") or {}, res.ok, res.text or "")
        except Exception:   # noqa: BLE001 —— 台账坏了不该连累工具调用
            pass
    progress_reporting.observe_tool_result(ctx, tc.get("name") or "", res)
    result = res.text
    payload = {"arguments": tc.get("arguments") or {}}
    if res.error:
        payload["traceback"] = res.error[:2000]
    traces.record(
        getattr(ctx, "session_id", ""), getattr(ctx, "turn_id", ""),
        "tool_call", tc["name"], ok=res.ok, duration_ms=duration_ms,
        summary=result[:300], payload=payload)
    progress_event = getattr(ctx, "progress_last_event", {}) if tc.get("name") == "progress_update" else {}
    if progress_event and res.ok:
        narrate(panels.render_progress(progress_event))
    else:
        narrate(ui.tool_result(result, ok=res.ok))
    _emit_safe(emit, stream_json.tool_result_event(
        getattr(ctx, "session_id", ""), tc["id"], result, not res.ok))
    # 步骤收尾事件：耗时在这里才拿得到（traces 记的也是这个数），一并进事件流，
    # UI 画执行时间线就不用再去 join traces.db。
    _emit_safe(emit, stream_json.step_event(
        getattr(ctx, "session_id", ""), getattr(ctx, "turn_id", ""),
        tc["id"], seq, tc["name"], tc.get("arguments") or {},
        "blocked" if blocked else ("ok" if res.ok else "error"), duration_ms))
    # 文件变更：工具拿不到 emit（它只在这一层），所以工具把改动记在 ctx 上，
    # 这里排空发出去。排在 step 收尾之后 —— 先说"这一步改完了"，再说"改了什么"。
    changes = getattr(ctx, "file_changes", None)
    if changes:
        for ch in changes:
            _emit_safe(emit, stream_json.file_change_event(
                getattr(ctx, "session_id", ""), getattr(ctx, "turn_id", ""),
                ch.get("path", ""), ch.get("action", ""), ch.get("diff", ""),
                ch.get("scope", "file")))
        changes.clear()
    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})


def _run_one(tc: dict, ctx: ToolContext, guard: "loop_guard.LoopGuard | None" = None):
    """执行一个工具调用，返回 (结果, 耗时ms, 是否被前置护栏拦下)。

    "被护栏拦下"和"工具真的失败了"对模型是一回事（都要改做法），对用户不是：
    前者是流程纠偏（先列计划再动手），后者才是出错。分开标出来，UI 才不会把
    一次正常的流程纠偏画成红叉。
    """
    started = time.time()
    guarded = _guard_tool_call(ctx, tc, guard)
    res = guarded or dispatch_result(tc["name"], tc["arguments"], ctx)
    return res, int((time.time() - started) * 1000), guarded is not None


def _dispatch_tool_calls(ctx: ToolContext, messages: list, status: TurnStatus, tool_calls: list,
                         step_idx: int, max_steps: int, narrate: Callable[[str], None],
                         emit: Callable[[dict], None] | None = None,
                         guard: "loop_guard.LoopGuard | None" = None) -> None:
    """派发本步所有工具调用：叙述、执行、记 trace、把结果按原顺序回灌到 messages。
    当本步全部是只读且并行安全的工具时并发执行（降延迟）；否则顺序执行（保留审批/写入语义）。"""
    parallel = len(tool_calls) > 1 and all(tc["name"] in PARALLEL_SAFE for tc in tool_calls)

    def announce(tc: dict) -> int:
        """叙述 + 发"开始"步骤事件，返回本步在整轮里的序号。

        序号取 status.tool_calls（record_tool_call 刚自增过），全轮单调递增，
        UI 靠它排时间线；配对靠 tc["id"]，与 tool_result 事件同一把钥匙。
        """
        status.record_tool_call(tc["name"])
        seq = status.tool_calls
        narrate(ui.tool_call(tc["name"], tc.get("arguments") or {}))
        _emit_safe(emit, stream_json.step_event(
            getattr(ctx, "session_id", ""), getattr(ctx, "turn_id", ""),
            tc["id"], seq, tc["name"], tc.get("arguments") or {}, "running"))
        return seq

    if parallel:
        # 并行分支：先把所有"开始"发出去（UI 同时亮起几枚芯片），再并发执行；
        # 收尾按原调用顺序回灌，与结果顺序保持一致。
        seqs = [announce(tc) for tc in tool_calls]
        with ThreadPoolExecutor(max_workers=min(len(tool_calls), 8)) as ex:
            outcomes = list(ex.map(lambda tc: _run_one(tc, ctx, guard), tool_calls))
        for tc, (res, dur, blocked), seq in zip(tool_calls, outcomes, seqs):
            _record_tool_result(ctx, messages, tc, res, dur, narrate, emit=emit, seq=seq,
                                blocked=blocked, guard=guard)
            status.observe_tool_result(tc["name"], res, tc.get("arguments"))
        return
    for tc in tool_calls:
        seq = announce(tc)
        res, dur, blocked = _run_one(tc, ctx, guard)
        _record_tool_result(ctx, messages, tc, res, dur, narrate, emit=emit, seq=seq,
                            blocked=blocked, guard=guard)
        status.observe_tool_result(tc["name"], res, tc.get("arguments"))


def _finalize_limit(ctx: ToolContext, messages: list, status: TurnStatus, max_steps: int,
                    extra_payload: dict | None = None) -> str:
    """到达步数上限时的统一收尾：写续跑提示、记任务中断、记 trace。"""
    text = _limit_payload(max_steps, status, ctx)
    _append_limit_context(messages, text)
    _record_task_interruption(ctx, text, status)
    _write_resume_artifact(ctx, status, text)
    payload = {"max_steps": max_steps, "tool_calls": status.tool_calls}
    if status.budget is not None:
        payload.update({"steps_used": status.budget.steps_used,
                        "steps_refunded": status.budget.steps_refunded,
                        "cost_cny": round(status.budget.cost_cny, 6),
                        "stop_reason": status.budget.stop_reason()})
    if extra_payload:
        payload.update(extra_payload)
    traces.record(getattr(ctx, "session_id", ""), getattr(ctx, "turn_id", ""),
                  "turn_limit", "tool_steps", ok=False, summary=text, payload=payload)
    return text


def _write_resume_artifact(ctx: ToolContext, status: TurnStatus, text: str) -> None:
    """把"停在哪、已经证明了什么"落盘，供 `/resume` 接着做。

    `_record_task_interruption` 只在绑了 task_id 时生效（serve 的任务台），命令行这条路
    此前撞上限就只剩一句提示文字 —— 计划状态和证据都在内存里，进程一退就没了。
    """
    session_id = getattr(ctx, "session_id", "") or ""
    if not session_id:
        return
    try:
        plan_store.mark_replan(
            session_id,
            f"上一轮未收尾就停了（{status.budget.stop_reason() or 'steps'}）。"
            "继续时先看计划里哪几步还没进终态，从那里接着做。")
    except Exception:   # noqa: BLE001
        pass


def _verify_gate_feedback(ctx: ToolContext, status: TurnStatus,
                          narrate: Callable[[str], None]) -> str | None:
    """本轮写过源码且模型想收尾时，跑完成前自验证门禁。返回注回文本(未通过)或 None(放行)。
    非代码轮/非 git 仓/门禁关/已达上限 → None。异常一律放行，绝不因门禁卡死主流程。"""
    if not status.wrote_code:
        return None
    if (status.behavioral_task and status.wrote_code_files
            and not status.runtime_validated and status.behavior_gate_rounds < 1):
        status.behavior_gate_rounds += 1
        narrate(ui.message("warn", "行为类改动尚未验证真实运行路径，不能直接收尾。"))
        return transcript.gate_text(
            transcript.COMPLETION_GATE,
            " 这是界面/输出/交互类任务。测试通过不等于目标行为已生效；"
            "请用 run_command 或 run_python 验证真实运行路径（至少运行一个最小可执行场景）并核对关键输出，再给最终结论。"
        )
    if status.verify_rounds >= _VERIFY_CAP:
        return None
    if not config.get_setting("verify_before_done", True):
        return None
    try:
        from . import verify
        res = verify.gate(getattr(ctx, "workspace", "") or ".",
                          run_tests=bool(config.get_setting("verify_run_tests", True)),
                          timeout=int(config.get_setting("verify_test_timeout", 120)))
    except Exception:   # noqa: BLE001
        return None
    if res.get("ok"):
        return None
    status.verify_rounds += 1
    narrate(ui.message("warn", "完成前自验证未通过，先修复再收尾。"))
    return res.get("feedback") or None


_CRITIQUE_CAP = 1          # 最多逼修正几轮。自查是帮手不是关卡，绝不因为它把一轮耗光。


def _critique_kind(ctx: ToolContext, status: TurnStatus) -> str:
    """这一轮该用哪套复核维度。"""
    if getattr(ctx, "executed_writes", False):
        return "ads"
    if status.wrote_code_files:
        return "code"
    if getattr(ctx, "knowledge_citations", []):
        return "knowledge"
    return "general"


#: 通过时给用户看的自查备注最多几条、每条多长。这是旁注不是报告，长了没人看。
_CRITIQUE_NOTE_ITEMS = 3
_CRITIQUE_NOTE_CHARS = 160


def _critique_note(markdown: str) -> str:
    """从复核正文里挑出实质发现，渲染成给用户看的几行旁注。没有实质内容返回空串。"""
    lines: list[str] = []
    for raw in (markdown or "").splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line or line.startswith("#"):
            continue
        if line.lstrip("*_ ").startswith(("通过", "建议修正")) or line in ("**通过**", "通过。"):
            continue
        if not re.match(r"^\d+[.、)]", line):
            continue
        body = re.sub(r"^\d+[.、)]\s*", "", line)
        # "未发现问题"这类空结论不值得占一行
        if any(mark in body for mark in ("未发现", "无问题", "没有问题", "无异常", "符合")):
            continue
        lines.append("  · " + body[:_CRITIQUE_NOTE_CHARS])
        if len(lines) >= _CRITIQUE_NOTE_ITEMS:
            break
    return "\n".join(lines)


def _critique_gate_feedback(ctx: ToolContext, status: TurnStatus, content: str,
                            narrate: Callable[[str], None]) -> str | None:
    """收尾前自查门禁：改过真代码、或真下过写指令的轮次，交付前由**运行时**复核一遍。

    为什么只盯这两种轮次：自查要多花一次模型调用，全轮次开等于给每次问答都加一份税。
    而"动过东西"的轮次是返工代价最高的地方 —— 改错了要再改一遍，广告下错了要花钱。
    普通问答、只写文档的轮次一律不碰。

    模型自己调过 `self_critique` 就不再代劳（它已经自查过了）。
    结论是"通过"时静默放行，只有"建议修正"才注回 —— 拿不准一律按通过（见 critique.needs_fix）。
    """
    if status.critique_rounds >= _CRITIQUE_CAP or status.self_critiqued:
        return None
    if not (status.wrote_code_files or getattr(ctx, "executed_writes", False)):
        return None
    if not config.get_setting("critique_before_done", True):
        return None
    provider = getattr(ctx, "provider", None)
    if provider is None or not (content or "").strip():
        return None
    kind = _critique_kind(ctx, status)
    narrate(ui.message("info", "改动已落盘，交付前先自查一遍…"))
    try:
        from . import critique as _crit
        res = _crit.critique(getattr(ctx, "progress_query", "") or "", content, provider, kind=kind)
    except Exception:   # noqa: BLE001 —— 自查挂了绝不拖累交付
        return None
    # 这一次调用是**运行时自己发起的**，用户看不见，所以更要计进成本 ——
    # 漏在闸外的话，「本轮花了多少钱」报出来的数就是假的。
    _charge_side_call(status, provider, res.get("usage"))
    traces.record(getattr(ctx, "session_id", ""), getattr(ctx, "turn_id", ""),
                  "critique", kind, ok=not res.get("needs_fix"),
                  summary=(res.get("markdown") or res.get("note") or "")[:1000])
    if not res.get("ok") or not res.get("needs_fix"):
        # 判"通过"不等于**什么都没发现**。实测里一次通过的复核仍然指出了
        # "调用方可能依赖 ZeroDivisionError"这种真问题 —— 静默丢掉等于付了钱没拿到东西。
        # 只讲给用户听（narrate），不注回模型：它已经决定通过了，再塞回去只会诱导它
        # 把对的答案改坏。
        note = _critique_note(res.get("markdown") or "")
        if note:
            narrate(ui.message("info", "自查备注（不影响结论）：\n" + note))
        return None
    status.critique_rounds += 1
    narrate(ui.message("warn", "收尾自查发现问题，先处理再交付。"))
    return transcript.gate_text(
        transcript.CRITIQUE_GATE,
        "\n" + (res.get("markdown") or "") +
        "\n\n请逐条处理：能改的直接改（改完照常验证），改不了的在最终回答里**明说**"
        "为什么不改、风险是什么。不要为了通过自查把已经对的结论改掉；"
        "确认某条批评不成立时，说明理由即可。"
    )


def _progress_gate_feedback(ctx: ToolContext, narrate: Callable[[str], None]) -> str | None:
    feedback = progress_reporting.completion_feedback(ctx)
    if feedback:
        narrate(ui.message("warn", "阶段或最终汇报尚未闭环，先补齐再收尾。"))
    return feedback


def _citation_gate_feedback(ctx: ToolContext, content: str, status: TurnStatus,
                            narrate: Callable[[str], None]) -> str | None:
    citations = list(getattr(ctx, "knowledge_citations", []) or [])
    if not citations:
        return None
    check = knowledge.validate_citations(content, citations)
    if check["ok"] or status.citation_gate_rounds >= 2:
        return None
    status.citation_gate_rounds += 1
    narrate(ui.message("warn", "亚马逊知识引用尚未通过校验，先绑定具体证据再收尾。"))
    available = ", ".join(f"[{key}]" for key in check["available"])
    invalid = ", ".join(f"[{key}]" for key in check["invalid"])
    detail = f" 未知引用：{invalid}。" if invalid else ""
    return transcript.gate_text(
        transcript.CITATION_GATE,
        f" 本轮已检索到证据 {available}，但当前答案没有有效、完整地引用它们。{detail}"
        "请重写最终答案：只在确实由摘录支持的事实句末标 [K#]；不得编造编号；"
        "官方事实、账户观测、分析推断、运营假设要分开表达；归因销售不等于增量销售，账户现象不等于官方算法。"
        "不要自行撰写来源清单，系统会生成。"
    )


def _finalize_citations(ctx: ToolContext, content: str) -> str:
    citations = list(getattr(ctx, "knowledge_citations", []) or [])
    if not citations:
        return content
    check = knowledge.validate_citations(content, citations)
    rendered = knowledge.append_citation_footer(content, citations)
    if check["ok"]:
        return rendered
    return rendered.rstrip() + "\n\n引用校验：知识已检索，但回答未完整绑定到有效引用，相关结论需人工复核。"


def _plan_note(ctx: ToolContext) -> str:
    """当前会话的 `[当前计划]` 文本。没有会话 id / 没有计划时返回空串。"""
    try:
        return plan_store.render_note(getattr(ctx, "session_id", "") or "")
    except Exception:   # noqa: BLE001 —— 计划台账坏了不能连累这一轮
        return ""


def _adopt_task_plan(ctx: ToolContext) -> None:
    """会话绑了长任务、计划却还空着时，把任务里排好的步骤搬进计划台账。

    放在 `task_scope.prepare_messages` **之后**：换一轮查询时 `progress_reporting.reset`
    会把计划清空，播种必须发生在那之后，否则刚种下就被清掉。也正因为清空之后会重新
    从任务文件播种，而任务文件一直在接收计划的投影，**已推进的进度能穿过 reset 活下来**。
    """
    task_id = getattr(ctx, "task_id", "") or ""
    if not task_id:
        return
    try:
        plan_store.adopt_task(getattr(ctx, "session_id", "") or "", task_id)
    except Exception:   # noqa: BLE001 —— 播种失败就当没绑任务，行为与改造前一致
        return


def _inject_plan_note(ctx: ToolContext, messages: list) -> None:
    """把计划注回最后一条 user 消息。

    与 `task_scope.prepare_messages` 同一个机制、同一个位置：追加到用户消息尾部，
    而不是新开一条 system —— 多条 system 在 Anthropic 那条路上会被 `_split_messages`
    切走，各家 provider 行为不一致，而追加到 user 是所有 provider 都保真的做法。
    """
    note = _plan_note(ctx)
    if not note:
        return
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        text = content if isinstance(content, str) else str(content or "")
        if plan_store.PLAN_NOTE_MARKER in text:
            return
        if isinstance(content, list):
            msg["content"] = list(content) + [{"type": "text", "text": note}]
        else:
            msg["content"] = (text + "\n\n" + note) if text else note
        return


def _charge_side_call(status: "TurnStatus | None", provider, usage) -> None:
    """把运行时自己发起的模型调用（自查、压缩）记进本轮成本。

    这些调用用户看不见、也不在工具时间线上，但钱是真花的。漏在闸外的话
    「本轮花了多少钱」就是个假数，而成本闸恰恰是靠这个数决定停不停。
    """
    if status is None or status.budget is None or not usage:
        return
    try:
        status.budget.add_cost(_step_cost(provider, usage))
    except Exception:   # noqa: BLE001
        return


def _maybe_compact(messages: list, provider, step_idx: int, narrate: Callable[[str], None],
                   ctx: ToolContext | None = None,
                   status: "TurnStatus | None" = None) -> None:
    """步边界上的轮内压缩守卫：此处 tool_call↔tool 已配对完整，整段替换安全。
    仅在估算 token 越过硬上限（防溢出）或开了自动压缩到软阈值时触发。

    压缩会把"我原本打算干几件事、干到第几件"一并摘要掉。计划不能交给摘要来保管 ——
    它是结构化状态，摘要是散文。所以这里把计划**原样**接在摘要后面一起保留。"""
    if step_idx == 0:
        return
    est = context.estimate_tokens(messages)
    if not context.should_compact_midturn(est):
        return
    # 越过阈值 ≠ 压得动。阈值被调到比 system 提示词还低时，用量永远在阈值之上，
    # 而压缩动不了 system —— 不加这道闸就是每一步都压、每一步都白压（见 context
    # 的 MIN_COMPACTIBLE_TOKENS）。这里除了跳过，还要**说一声**：默不作声地忽略
    # 用户设的阈值，比压错更难查。
    if not context.worth_compacting(messages):
        if status is not None and not status.compact_warned:
            status.compact_warned = True
            narrate(ui.message(
                "warn",
                f"上下文约 {est} tok 已越过压缩阈值，但可压缩的历史不足 "
                f"{context.MIN_COMPACTIBLE_TOKENS} tok —— 占用主要来自 system 提示词与"
                "工具定义，压缩帮不上忙，本轮跳过。若长期如此，请把 compact_at_tokens "
                "调到 system 提示词之上（config set compact_at_tokens <n>）。",
            ))
        return
    new, summary, usage = context.compact(messages, provider,
                                          extra_note=_plan_note(ctx) if ctx is not None else "",
                                          return_usage=True)
    if summary:
        messages[:] = new
        _charge_side_call(status, provider, usage)
        narrate(ui.message("info", f"上下文已自动压缩（约 {est} tok）以防溢出，继续。"))


def run_turn(provider: LLMProvider, ctx: ToolContext, messages: list,
             max_steps: int | None = None, narrate: Callable[[str], None] = print,
             tools: list | None = None) -> str:
    """跑一轮对话（messages 含 system+历史+本次 user）。就地追加消息，返回最终回答。
    tools 可传受限工具子集（如只读子 agent）；传 [] = 不挂工具（纯文本生成）；
    None = 全量 TOOL_SCHEMAS。"""
    task_scope.prepare_messages(ctx, messages)
    _adopt_task_plan(ctx)
    _inject_plan_note(ctx, messages)
    thinking.apply_to(provider, ctx)
    tool_schemas = TOOL_SCHEMAS if tools is None else tools
    max_steps = _resolve_max_steps(max_steps, "chat_max_tool_steps")
    turn_budget = budget_mod.from_settings(max_steps)
    status = TurnStatus(max_steps=max_steps, budget=turn_budget,
                        behavioral_task=bool(getattr(ctx, "behavioral_task", False)))
    guard = _new_loop_guard()
    _ceiling = _hard_step_ceiling(max_steps)
    for step_idx in range(_ceiling):
        if turn_budget.exhausted():
            break
        if step_idx == _ceiling - 1:
            # 最后一格：跑完这一步就没了，且预算还没用完 —— 记成撞天花板，
            # 免得收尾文案把它说成"步数上限"并给出一条没用的建议。
            turn_budget.mark_ceiling()
        _maybe_compact(messages, provider, step_idx, narrate, ctx, status)
        status.before_model_step(step_idx, narrate)
        msg = provider.chat(messages, tools=tool_schemas)
        turn_budget.add_cost(_step_cost(provider, msg.get("usage") or {}))
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            content = msg.get("content", "") or ""
            messages.append({"role": "assistant", "content": content})
            fb = _verify_gate_feedback(ctx, status, narrate)
            if fb is None:
                fb = _progress_gate_feedback(ctx, narrate)
            if fb is None:
                fb = _citation_gate_feedback(ctx, content, status, narrate)
            if fb is None:
                fb = _critique_gate_feedback(ctx, status, content, narrate)
            if fb is not None:
                messages.append({"role": "user", "content": fb})
                continue
            content = _finalize_citations(ctx, content)
            messages[-1]["content"] = content
            return content
        _append_tool_call_msg(messages, msg.get("content"), tool_calls)
        _dispatch_tool_calls(ctx, messages, status, tool_calls, step_idx, max_steps, narrate,
                             guard=guard)
    return _finalize_limit(ctx, messages, status, max_steps)


def run_turn_stream(provider: LLMProvider, ctx: ToolContext, messages: list,
                    max_steps: int | None = None, narrate: Callable[[str], None] = print,
                    render: Callable[[str], None] = None, model: str = "",
                    cancel_check: Callable[[], bool] | None = None,
                    render_reasoning: Callable[[str], None] = None,
                    emit: Callable[[dict], None] | None = None,
                    tools: list | None = None,
                    defer_citation_text: bool = True,
                    on_answer_reset: Callable[[str], None] | None = None) -> dict:
    """流式跑一轮：token 边出边渲染、工具实时叙述、累计用量。
    返回 {text, usage}（usage 为本轮各步累加）。render(token) 逐字输出助手文本。

    render_reasoning(token)：支持思考的模型(deepseek-reasoner/codex/claude/gemini)的
    思考流；默认无操作(不显示)。cancel_check：TUI 忙碌时请求中断的钩子；在步/流/工具边界
    返回 True 则抛 KeyboardInterrupt，交给上层保留会话并恢复输入。
    emit(event)：结构化事件回调（stream-json），每个模型步发一条 assistant 事件、
    每个工具结果发一条 tool_result 事件；默认 None 零开销。
    tools：受限工具子集（与 run_turn 对齐）；[] = 不挂工具，None = 全量 TOOL_SCHEMAS。
    defer_citation_text：带 [K#] 知识引证时是否把正文压到引证门通过后一次性输出。
    终端（CLI）保持 True——中间草稿打出去收不回来；Web/serve 传 False 边生成边流式，
    前端以 final 事件的 text 为准整体替换，引证重写不会留下脏文本。

    on_answer_reset(reason)：**上一段已经渲染出去的正文作废了**，从这里开始的是新
    一稿。一轮里模型可能把正文吐好几遍——工具前的开场白、门禁打回后的整篇重写
    （引用校验最多来回 2 次）——终端是一条向下的日志、叠着看没问题，但网页把
    token 顺序拼进同一个气泡，用户看到的就是同一张表连出三遍。给了这个回调的
    调用方（serve → 网页）会收到边界，把气泡清空重画；不给（CLI）则一个字都不变。
    reason: tool_call | gate:verify | gate:progress | gate:citation | gate:critique。"""
    task_scope.prepare_messages(ctx, messages)
    _adopt_task_plan(ctx)
    _inject_plan_note(ctx, messages)
    thinking.apply_to(provider, ctx)
    tool_schemas = TOOL_SCHEMAS if tools is None else tools
    render = render or (lambda s: print(s, end="", flush=True))
    render_reasoning = render_reasoning or (lambda s: None)
    cancel_check = cancel_check or (lambda: False)
    # llm_ms：**只**覆盖模型流式窗口（下面那圈 provider.stream_chat）。工具执行在窗口之外，
    # 所以这个数回答的是"这一轮里有多少时间真的花在模型上"——一轮 20 分钟里模型只占 40 秒
    # 和占 18 分钟，要做的优化完全不是一回事，而此前调用方只拿得到一个总时长。
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0,
                   "llm_ms": 0}

    # 正文的"稿"边界。发通知的时刻是**新一稿的第一个字**——在那之前谁也不知道
    # 模型还会不会再写一遍，提前发就会把还在用的那一稿误清。
    rendered_any = False        # 本轮已经渲染过正文
    draft_open = False          # 当前这一步已经开了稿（每步开头复位）
    superseded_by = ""          # 上一稿是被什么作废的

    def _render_text(chunk: str) -> None:
        nonlocal rendered_any, draft_open
        if not draft_open:
            draft_open = True
            if rendered_any and on_answer_reset is not None:
                try:
                    on_answer_reset(superseded_by or "restart")
                except Exception:   # noqa: BLE001 — 通知失败绝不能打断正在跑的轮次
                    pass
        rendered_any = True
        render(chunk)

    def _accum(u: dict) -> None:
        if not u:
            return
        total_usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        total_usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        total_usage["prompt_cache_hit_tokens"] += int(
            u.get("prompt_cache_hit_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)

    max_steps = _resolve_max_steps(max_steps, "chat_max_tool_steps")
    turn_budget = budget_mod.from_settings(max_steps)
    status = TurnStatus(max_steps=max_steps, budget=turn_budget,
                        behavioral_task=bool(getattr(ctx, "behavioral_task", False)))
    guard = _new_loop_guard()
    _ceiling = _hard_step_ceiling(max_steps)
    for step_idx in range(_ceiling):
        if turn_budget.exhausted():
            break
        if step_idx == _ceiling - 1:
            # 最后一格：跑完这一步就没了，且预算还没用完 —— 记成撞天花板，
            # 免得收尾文案把它说成"步数上限"并给出一条没用的建议。
            turn_budget.mark_ceiling()
        if cancel_check():
            raise KeyboardInterrupt
        _maybe_compact(messages, provider, step_idx, narrate, ctx, status)
        status.before_model_step(step_idx, narrate)
        final = {"content": "", "tool_calls": [], "usage": {}}
        printed_any = False
        draft_open = False
        buffered_text: list[str] = []
        defer_text = bool(
            (
                getattr(ctx, "progress_required", False)
                and not getattr(ctx, "progress_final", {})
                and not getattr(ctx, "plan_mode", False)
            )
            or (defer_citation_text and bool(getattr(ctx, "knowledge_citations", [])))
        )
        llm_started = time.monotonic()
        for ev in provider.stream_chat(messages, tools=tool_schemas):
            if cancel_check():
                raise KeyboardInterrupt
            if ev["type"] == "text":
                printed_any = True
                buffered_text.append(ev["text"])
                if not defer_text:
                    _render_text(ev["text"])
            elif ev["type"] == "reasoning":
                render_reasoning(ev.get("text") or "")
            elif ev["type"] == "final":
                final = ev
        # 渲染回调（终端打印 / serve 推 SSE）落在这个窗口里，因为它们本来就是边收边发的
        # 一部分，拆不开也不该拆——窗口量的是"模型这一步从开口到闭嘴用了多久"。
        total_usage["llm_ms"] += int((time.monotonic() - llm_started) * 1000)
        if printed_any and not defer_text:
            render("\n")
        _accum(final.get("usage") or {})
        turn_budget.add_cost(_step_cost(provider, final.get("usage") or {}, model))
        tool_calls = final.get("tool_calls") or []
        if not tool_calls:
            content = final.get("content", "") or ""
            messages.append({"role": "assistant", "content": content})
            gate = ""
            fb = _verify_gate_feedback(ctx, status, narrate)
            if fb is not None:
                gate = "verify"
            if fb is None:
                fb = _progress_gate_feedback(ctx, narrate)
                if fb is not None:
                    gate = "progress"
            if fb is None:
                fb = _citation_gate_feedback(ctx, content, status, narrate)
                if fb is not None:
                    gate = "citation"
            if fb is None:
                fb = _critique_gate_feedback(ctx, status, content, narrate)
                if fb is not None:
                    gate = "critique"
            if fb is not None:
                messages.append({"role": "user", "content": fb})
                # 门禁要求的是**整篇重写**，所以刚吐出去的那一稿到此作废。
                superseded_by = f"gate:{gate}"
                continue
            content = _finalize_citations(ctx, content)
            messages[-1]["content"] = content
            if defer_text and content:
                _render_text(content)
                render("\n")
            _emit_safe(emit, stream_json.assistant_event(
                getattr(ctx, "session_id", ""), content, []))
            return {"text": content, "usage": total_usage}
        if cancel_check():
            raise KeyboardInterrupt
        _emit_safe(emit, stream_json.assistant_event(
            getattr(ctx, "session_id", ""), final.get("content") or "", tool_calls))
        # 这一步的正文是"工具前的开场白"。它不是答案，等下一稿开始时该让位。
        superseded_by = "tool_call"
        _append_tool_call_msg(messages, final.get("content"), tool_calls)
        _dispatch_tool_calls(ctx, messages, status, tool_calls, step_idx, max_steps, narrate,
                             emit=emit, guard=guard)
    text = _finalize_limit(ctx, messages, status, max_steps, extra_payload={"usage": total_usage})
    return {"text": text, "usage": total_usage}
