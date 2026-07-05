"""对话式 Agent 循环（ReAct 工具调用）。

provider.chat(messages, tools) → 若有 tool_calls 则逐个派发(写工具内部走人工
审批)、把结果回灌 → 直到模型给出最终回答。带步数上限防失控。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, context, panels, progress_reporting, stream_json, task_scope, traces, ui
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
规划与汇报：多步/复杂任务**动手前先用 todo_write 拆成可验证的小步**，再调用 progress_update(kind=start) 向用户说明目标、范围、阶段、完成标准和第一阶段准备做什么；这两项完成前不要调用实际工作工具。执行时同一时间恰好一个 in_progress。每阶段结束先 progress_update(kind=phase_end) 汇报做了什么、状态、证据、未完成和注意事项，再把 Todo 标 completed/blocked/skipped；下一阶段先更新 Todo，再 progress_update(kind=phase_start) 介绍准备做什么。全部结束后必须 progress_update(kind=final)，汇总已做到、未做到、验证和注意事项，再用一句简短正文收尾。单步、明确的小任务别过度汇报。UI/行为类改动，typecheck/编译/测试通过 ≠ 完成，必须在真实界面或运行环境复现目标场景确认后才算完成。
澄清：当需求**歧义、有多种合理理解、或缺关键输入（ASIN/路径/目标/站点等）**时，先用一两个精准问题反问、停下等用户回答，**别靠假设硬做**；信息足够才进入执行。但简单明确的任务别来回追问。
原则：先拿证据再动手；写操作一律经人工审批，绝不自作主张直接写；动作绑数据、简洁可执行；不要瞎编 ASIN/规格/数字。读文件优先用 read_file 看真实内容，不要假设；**大文件读某几行用 read_file 的 offset/limit，别用 run_command/python 分段读**（那会反复弹审批）。"""

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

DEFAULT_MAX_TOOL_STEPS = 48
DEFAULT_TOOL_WARNING_REMAINING = 5
CODE_WRITE_TOOLS = {"write_file", "edit_file", "code_apply_patch"}   # 触发完成前自验证门禁的写工具
_VERIFY_CAP = 2                                                       # 门禁最多逼修复几轮，防失控
_NAVIGATION_TOOLS = {"grep", "glob", "code_search", "code_symbols", "code_impact"}
_PROJECT_MUTATION_TOOLS = CODE_WRITE_TOOLS | {"run_command", "run_python", "run_tests"}
_RUNTIME_VALIDATION_TOOLS = {"run_command", "run_python", "bash_output"}
_MAX_NAVIGATION_WITHOUT_READ = 8


@dataclass
class TurnStatus:
    max_steps: int
    warning_remaining: int = DEFAULT_TOOL_WARNING_REMAINING
    warned: bool = False
    tool_calls: int = 0
    wrote_code: bool = False       # 本轮是否动过源码（决定收尾前是否走自验证门禁）
    verify_rounds: int = 0         # 已触发的自验证逼修复轮数
    behavioral_task: bool = False  # UI/输出/行为任务：测试之外还需要运行路径证据
    runtime_validated: bool = False
    behavior_gate_rounds: int = 0

    def before_model_step(self, step_idx: int, narrate: Callable[[str], None]) -> None:
        remaining = self.max_steps - step_idx
        if not self.warned and remaining <= self.warning_remaining:
            self.warned = True
            narrate(ui.message(
                "warn",
                f"工具预算剩余 {remaining}/{self.max_steps} 步；我会优先收敛结果，必要时请说“继续”。"
                "若进展不顺，先停下重列假设/换定位思路，别把疑似错误的路径走到底。",
            ))

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def observe_tool_result(self, name: str, result: ToolResult) -> None:
        text = result.text or ""
        if name in CODE_WRITE_TOOLS:
            blocked = any(marker in text for marker in (
                "已拦截", "计划模式", "未找到要替换", "写入失败", "编辑失败", "ok: False",
            ))
            if result.ok and not blocked:
                self.wrote_code = True
                self.runtime_validated = False
                self.behavior_gate_rounds = 0
            return
        if not self.wrote_code or name not in _RUNTIME_VALIDATION_TOOLS or not result.ok:
            return
        if "退出码 0" in text or "returncode=0" in text or "已结束（exit=0" in text:
            self.runtime_validated = True


def _resolve_max_steps(value: int | None, setting_key: str) -> int:
    if value is not None:
        return max(1, int(value))
    try:
        return max(1, int(config.get_setting(setting_key, DEFAULT_MAX_TOOL_STEPS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOOL_STEPS


def _limit_text(max_steps: int) -> str:
    return (f"（已达到本轮工具调用步数上限 {max_steps}。可以直接说“继续”，"
            "或执行 `ivyea config set chat_max_tool_steps 80` 提高单轮上限。）")


def _limit_payload(max_steps: int, status: TurnStatus, ctx: ToolContext | None = None) -> str:
    text = (
        f"{_limit_text(max_steps)}\n"
        f"本轮已经执行工具调用 {status.tool_calls} 次。下一轮继续时，请先总结已完成的工具结果，"
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


def _guard_tool_call(ctx: ToolContext, tc: dict) -> ToolResult | None:
    """Enforce scope/search recovery even when the model ignores prompt instructions."""
    name = tc.get("name") or ""
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
                        emit: Callable[[dict], None] | None = None) -> None:
    _observe_tool_discipline(ctx, tc, res)
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
    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})


def _run_one(tc: dict, ctx: ToolContext):
    started = time.time()
    res = _guard_tool_call(ctx, tc) or dispatch_result(tc["name"], tc["arguments"], ctx)
    return res, int((time.time() - started) * 1000)


def _dispatch_tool_calls(ctx: ToolContext, messages: list, status: TurnStatus, tool_calls: list,
                         step_idx: int, max_steps: int, narrate: Callable[[str], None],
                         emit: Callable[[dict], None] | None = None) -> None:
    """派发本步所有工具调用：叙述、执行、记 trace、把结果按原顺序回灌到 messages。
    当本步全部是只读且并行安全的工具时并发执行（降延迟）；否则顺序执行（保留审批/写入语义）。"""
    parallel = len(tool_calls) > 1 and all(tc["name"] in PARALLEL_SAFE for tc in tool_calls)
    if parallel:
        for tc in tool_calls:
            status.record_tool_call()
            narrate(ui.tool_call(tc["name"], tc.get("arguments") or {}))
        with ThreadPoolExecutor(max_workers=min(len(tool_calls), 8)) as ex:
            outcomes = list(ex.map(lambda tc: _run_one(tc, ctx), tool_calls))
        for tc, (res, dur) in zip(tool_calls, outcomes):
            _record_tool_result(ctx, messages, tc, res, dur, narrate, emit=emit)
            status.observe_tool_result(tc["name"], res)
        return
    for tc in tool_calls:
        status.record_tool_call()
        narrate(ui.tool_call(tc["name"], tc.get("arguments") or {}))
        res, dur = _run_one(tc, ctx)
        _record_tool_result(ctx, messages, tc, res, dur, narrate, emit=emit)
        status.observe_tool_result(tc["name"], res)


def _finalize_limit(ctx: ToolContext, messages: list, status: TurnStatus, max_steps: int,
                    extra_payload: dict | None = None) -> str:
    """到达步数上限时的统一收尾：写续跑提示、记任务中断、记 trace。"""
    text = _limit_payload(max_steps, status, ctx)
    _append_limit_context(messages, text)
    _record_task_interruption(ctx, text, status)
    payload = {"max_steps": max_steps, "tool_calls": status.tool_calls}
    if extra_payload:
        payload.update(extra_payload)
    traces.record(getattr(ctx, "session_id", ""), getattr(ctx, "turn_id", ""),
                  "turn_limit", "tool_steps", ok=False, summary=text, payload=payload)
    return text


def _verify_gate_feedback(ctx: ToolContext, status: TurnStatus,
                          narrate: Callable[[str], None]) -> str | None:
    """本轮写过源码且模型想收尾时，跑完成前自验证门禁。返回注回文本(未通过)或 None(放行)。
    非代码轮/非 git 仓/门禁关/已达上限 → None。异常一律放行，绝不因门禁卡死主流程。"""
    if not status.wrote_code:
        return None
    if status.behavioral_task and not status.runtime_validated and status.behavior_gate_rounds < 1:
        status.behavior_gate_rounds += 1
        narrate(ui.message("warn", "行为类改动尚未验证真实运行路径，不能直接收尾。"))
        return (
            "[完成门禁] 这是界面/输出/交互类任务。测试通过不等于目标行为已生效；"
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


def _progress_gate_feedback(ctx: ToolContext, narrate: Callable[[str], None]) -> str | None:
    feedback = progress_reporting.completion_feedback(ctx)
    if feedback:
        narrate(ui.message("warn", "阶段或最终汇报尚未闭环，先补齐再收尾。"))
    return feedback


def _maybe_compact(messages: list, provider, step_idx: int, narrate: Callable[[str], None]) -> None:
    """步边界上的轮内压缩守卫：此处 tool_call↔tool 已配对完整，整段替换安全。
    仅在估算 token 越过硬上限（防溢出）或开了自动压缩到软阈值时触发。"""
    if step_idx == 0:
        return
    est = context.estimate_tokens(messages)
    if not context.should_compact_midturn(est):
        return
    new, summary = context.compact(messages, provider)
    if summary:
        messages[:] = new
        narrate(ui.message("info", f"上下文已自动压缩（约 {est} tok）以防溢出，继续。"))


def run_turn(provider: LLMProvider, ctx: ToolContext, messages: list,
             max_steps: int | None = None, narrate: Callable[[str], None] = print,
             tools: list | None = None) -> str:
    """跑一轮对话（messages 含 system+历史+本次 user）。就地追加消息，返回最终回答。
    tools 可传受限工具子集（如只读子 agent）；默认用全量 TOOL_SCHEMAS。"""
    task_scope.prepare_messages(ctx, messages)
    tool_schemas = tools or TOOL_SCHEMAS
    max_steps = _resolve_max_steps(max_steps, "chat_max_tool_steps")
    status = TurnStatus(max_steps=max_steps, behavioral_task=bool(getattr(ctx, "behavioral_task", False)))
    for step_idx in range(max_steps):
        _maybe_compact(messages, provider, step_idx, narrate)
        status.before_model_step(step_idx, narrate)
        msg = provider.chat(messages, tools=tool_schemas)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            content = msg.get("content", "") or ""
            messages.append({"role": "assistant", "content": content})
            fb = _verify_gate_feedback(ctx, status, narrate)
            if fb is None:
                fb = _progress_gate_feedback(ctx, narrate)
            if fb is not None:
                messages.append({"role": "user", "content": fb})
                continue
            return content
        _append_tool_call_msg(messages, msg.get("content"), tool_calls)
        _dispatch_tool_calls(ctx, messages, status, tool_calls, step_idx, max_steps, narrate)
    return _finalize_limit(ctx, messages, status, max_steps)


def run_turn_stream(provider: LLMProvider, ctx: ToolContext, messages: list,
                    max_steps: int | None = None, narrate: Callable[[str], None] = print,
                    render: Callable[[str], None] = None, model: str = "",
                    cancel_check: Callable[[], bool] | None = None,
                    render_reasoning: Callable[[str], None] = None,
                    emit: Callable[[dict], None] | None = None,
                    tools: list | None = None) -> dict:
    """流式跑一轮：token 边出边渲染、工具实时叙述、累计用量。
    返回 {text, usage}（usage 为本轮各步累加）。render(token) 逐字输出助手文本。

    render_reasoning(token)：支持思考的模型(deepseek-reasoner/codex/claude/gemini)的
    思考流；默认无操作(不显示)。cancel_check：TUI 忙碌时请求中断的钩子；在步/流/工具边界
    返回 True 则抛 KeyboardInterrupt，交给上层保留会话并恢复输入。
    emit(event)：结构化事件回调（stream-json），每个模型步发一条 assistant 事件、
    每个工具结果发一条 tool_result 事件；默认 None 零开销。
    tools：受限工具子集（与 run_turn 对齐）；默认全量 TOOL_SCHEMAS。"""
    task_scope.prepare_messages(ctx, messages)
    tool_schemas = tools or TOOL_SCHEMAS
    render = render or (lambda s: print(s, end="", flush=True))
    render_reasoning = render_reasoning or (lambda s: None)
    cancel_check = cancel_check or (lambda: False)
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0}

    def _accum(u: dict) -> None:
        if not u:
            return
        total_usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        total_usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        total_usage["prompt_cache_hit_tokens"] += int(
            u.get("prompt_cache_hit_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)

    max_steps = _resolve_max_steps(max_steps, "chat_max_tool_steps")
    status = TurnStatus(max_steps=max_steps, behavioral_task=bool(getattr(ctx, "behavioral_task", False)))
    for step_idx in range(max_steps):
        if cancel_check():
            raise KeyboardInterrupt
        _maybe_compact(messages, provider, step_idx, narrate)
        status.before_model_step(step_idx, narrate)
        final = {"content": "", "tool_calls": [], "usage": {}}
        printed_any = False
        buffered_text: list[str] = []
        defer_text = bool(
            getattr(ctx, "progress_required", False)
            and not getattr(ctx, "progress_final", {})
            and not getattr(ctx, "plan_mode", False)
        )
        for ev in provider.stream_chat(messages, tools=tool_schemas):
            if cancel_check():
                raise KeyboardInterrupt
            if ev["type"] == "text":
                printed_any = True
                buffered_text.append(ev["text"])
                if not defer_text:
                    render(ev["text"])
            elif ev["type"] == "reasoning":
                render_reasoning(ev.get("text") or "")
            elif ev["type"] == "final":
                final = ev
        if printed_any and not defer_text:
            render("\n")
        _accum(final.get("usage") or {})
        tool_calls = final.get("tool_calls") or []
        if not tool_calls:
            content = final.get("content", "") or ""
            messages.append({"role": "assistant", "content": content})
            fb = _verify_gate_feedback(ctx, status, narrate)
            if fb is None:
                fb = _progress_gate_feedback(ctx, narrate)
            if fb is not None:
                messages.append({"role": "user", "content": fb})
                continue
            if defer_text and printed_any:
                render("".join(buffered_text) or content)
                render("\n")
            _emit_safe(emit, stream_json.assistant_event(
                getattr(ctx, "session_id", ""), content, []))
            return {"text": content, "usage": total_usage}
        if cancel_check():
            raise KeyboardInterrupt
        _emit_safe(emit, stream_json.assistant_event(
            getattr(ctx, "session_id", ""), final.get("content") or "", tool_calls))
        _append_tool_call_msg(messages, final.get("content"), tool_calls)
        _dispatch_tool_calls(ctx, messages, status, tool_calls, step_idx, max_steps, narrate, emit=emit)
    text = _finalize_limit(ctx, messages, status, max_steps, extra_payload={"usage": total_usage})
    return {"text": text, "usage": total_usage}
