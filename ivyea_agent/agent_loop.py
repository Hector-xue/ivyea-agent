"""对话式 Agent 循环（ReAct 工具调用）。

provider.chat(messages, tools) → 若有 tool_calls 则逐个派发(写工具内部走人工
审批)、把结果回灌 → 直到模型给出最终回答。带步数上限防失控。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from . import config, traces, ui
from .agent_tools import TOOL_SCHEMAS, ToolContext, dispatch
from .providers import LLMProvider

SYSTEM_PROMPT = """你是 Ivyea Agent，一个亚马逊运营助手。专长是广告巡检，也能处理日常运营杂活。
广告：run_patrol(巡检) → propose_actions(看动作) → execute_actions(逐条人工审批执行) → 必要时 rollback。
通用：read_file/list_dir/web_fetch/web_search 读取信息；write_file/edit_file 产出文件；run_python(可用 pandas/openpyxl 读 Excel、算数)、run_command 执行——这些写/执行操作都会弹人工审批。
原则：先拿证据再动手；写操作一律经人工审批，绝不自作主张直接写；动作绑数据、简洁可执行；信息不足就澄清，不要瞎编 ASIN/规格/数字。读文件优先用 read_file 看真实内容，不要假设。"""

PLAN_NOTE = ("\n\n[计划模式] 当前为只读计划模式：可以巡检/分析/提动作，但**不要调用 execute_actions 写入**。"
             "先给出清晰的行动计划，待用户 /approve 批准后再执行。")

DEFAULT_MAX_TOOL_STEPS = 48
DEFAULT_TOOL_WARNING_REMAINING = 5


@dataclass
class TurnStatus:
    max_steps: int
    warning_remaining: int = DEFAULT_TOOL_WARNING_REMAINING
    warned: bool = False
    tool_calls: int = 0

    def before_model_step(self, step_idx: int, narrate: Callable[[str], None]) -> None:
        remaining = self.max_steps - step_idx
        if not self.warned and remaining <= self.warning_remaining:
            self.warned = True
            narrate(ui.message(
                "warn",
                f"工具预算剩余 {remaining}/{self.max_steps} 步；我会优先收敛结果，必要时请说“继续”。",
            ))

    def record_tool_call(self) -> None:
        self.tool_calls += 1


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


def _limit_payload(max_steps: int, status: TurnStatus) -> str:
    return (
        f"{_limit_text(max_steps)}\n"
        f"本轮已经执行工具调用 {status.tool_calls} 次。下一轮继续时，请先总结已完成的工具结果，"
        "再从最后一个未完成的小步骤继续；除非必要，不要重复已经成功的工具调用。"
    )


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


def run_turn(provider: LLMProvider, ctx: ToolContext, messages: list,
             max_steps: int | None = None, narrate: Callable[[str], None] = print) -> str:
    """跑一轮对话（messages 含 system+历史+本次 user）。就地追加消息，返回最终回答。"""
    max_steps = _resolve_max_steps(max_steps, "chat_max_tool_steps")
    status = TurnStatus(max_steps=max_steps)
    for step_idx in range(max_steps):
        status.before_model_step(step_idx, narrate)
        msg = provider.chat(messages, tools=TOOL_SCHEMAS)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            content = msg.get("content", "") or ""
            messages.append({"role": "assistant", "content": content})
            return content
        # 回灌助手的 tool_calls 消息（OpenAI 格式）
        messages.append({
            "role": "assistant", "content": msg.get("content") or None,
            "tool_calls": [{"id": tc["id"], "type": "function",
                            "function": {"name": tc["name"],
                                         "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                           for tc in tool_calls],
        })
        for call_idx, tc in enumerate(tool_calls, start=1):
            status.record_tool_call()
            narrate(ui.tool_call(tc["name"], tc.get("arguments") or {},
                                 step=f"{step_idx + 1}/{max_steps}.{call_idx}"))
            started = time.time()
            result = dispatch(tc["name"], tc["arguments"], ctx)
            traces.record(
                getattr(ctx, "session_id", ""),
                getattr(ctx, "turn_id", ""),
                "tool_call",
                tc["name"],
                ok=not result.startswith("工具 ") and "执行出错" not in result,
                duration_ms=int((time.time() - started) * 1000),
                summary=result[:300],
                payload={"arguments": tc.get("arguments") or {}},
            )
            narrate(ui.tool_result(result))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    text = _limit_payload(max_steps, status)
    _append_limit_context(messages, text)
    _record_task_interruption(ctx, text, status)
    traces.record(
        getattr(ctx, "session_id", ""),
        getattr(ctx, "turn_id", ""),
        "turn_limit",
        "tool_steps",
        ok=False,
        summary=text,
        payload={"max_steps": max_steps, "tool_calls": status.tool_calls},
    )
    return text


def run_turn_stream(provider: LLMProvider, ctx: ToolContext, messages: list,
                    max_steps: int | None = None, narrate: Callable[[str], None] = print,
                    render: Callable[[str], None] = None, model: str = "") -> dict:
    """流式跑一轮：token 边出边渲染、工具实时叙述、累计用量。
    返回 {text, usage}（usage 为本轮各步累加）。render(token) 逐字输出助手文本。"""
    render = render or (lambda s: print(s, end="", flush=True))
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0}

    def _accum(u: dict) -> None:
        if not u:
            return
        total_usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        total_usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        total_usage["prompt_cache_hit_tokens"] += int(
            u.get("prompt_cache_hit_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)

    max_steps = _resolve_max_steps(max_steps, "chat_max_tool_steps")
    status = TurnStatus(max_steps=max_steps)
    for step_idx in range(max_steps):
        status.before_model_step(step_idx, narrate)
        final = {"content": "", "tool_calls": [], "usage": {}}
        printed_any = False
        for ev in provider.stream_chat(messages, tools=TOOL_SCHEMAS):
            if ev["type"] == "text":
                printed_any = True
                render(ev["text"])
            elif ev["type"] == "final":
                final = ev
        if printed_any:
            render("\n")
        _accum(final.get("usage") or {})
        tool_calls = final.get("tool_calls") or []
        if not tool_calls:
            content = final.get("content", "") or ""
            messages.append({"role": "assistant", "content": content})
            return {"text": content, "usage": total_usage}
        messages.append({
            "role": "assistant", "content": final.get("content") or None,
            "tool_calls": [{"id": tc["id"], "type": "function",
                            "function": {"name": tc["name"],
                                         "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                           for tc in tool_calls],
        })
        for call_idx, tc in enumerate(tool_calls, start=1):
            status.record_tool_call()
            narrate(ui.tool_call(tc["name"], tc.get("arguments") or {},
                                 step=f"{step_idx + 1}/{max_steps}.{call_idx}"))
            started = time.time()
            result = dispatch(tc["name"], tc["arguments"], ctx)
            traces.record(
                getattr(ctx, "session_id", ""),
                getattr(ctx, "turn_id", ""),
                "tool_call",
                tc["name"],
                ok=not result.startswith("工具 ") and "执行出错" not in result,
                duration_ms=int((time.time() - started) * 1000),
                summary=result[:300],
                payload={"arguments": tc.get("arguments") or {}},
            )
            narrate(ui.tool_result(result))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    text = _limit_payload(max_steps, status)
    _append_limit_context(messages, text)
    _record_task_interruption(ctx, text, status)
    traces.record(
        getattr(ctx, "session_id", ""),
        getattr(ctx, "turn_id", ""),
        "turn_limit",
        "tool_steps",
        ok=False,
        summary=text,
        payload={"max_steps": max_steps, "tool_calls": status.tool_calls, "usage": total_usage},
    )
    return {"text": text, "usage": total_usage}
