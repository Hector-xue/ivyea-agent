"""对话式 Agent 循环（ReAct 工具调用）。

provider.chat(messages, tools) → 若有 tool_calls 则逐个派发(写工具内部走人工
审批)、把结果回灌 → 直到模型给出最终回答。带步数上限防失控。
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from .agent_tools import TOOL_SCHEMAS, ToolContext, dispatch
from .providers import LLMProvider

SYSTEM_PROMPT = """你是 Ivyea Agent，一个亚马逊运营助手。专长是广告巡检，也能处理日常运营杂活。
广告：run_patrol(巡检) → propose_actions(看动作) → execute_actions(逐条人工审批执行) → 必要时 rollback。
通用：read_file/list_dir/web_fetch/web_search 读取信息；write_file/edit_file 产出文件；run_python(可用 pandas/openpyxl 读 Excel、算数)、run_command 执行——这些写/执行操作都会弹人工审批。
原则：先拿证据再动手；写操作一律经人工审批，绝不自作主张直接写；动作绑数据、简洁可执行；信息不足就澄清，不要瞎编 ASIN/规格/数字。读文件优先用 read_file 看真实内容，不要假设。"""

PLAN_NOTE = ("\n\n[计划模式] 当前为只读计划模式：可以巡检/分析/提动作，但**不要调用 execute_actions 写入**。"
             "先给出清晰的行动计划，待用户 /approve 批准后再执行。")


def run_turn(provider: LLMProvider, ctx: ToolContext, messages: list,
             max_steps: int = 6, narrate: Callable[[str], None] = print) -> str:
    """跑一轮对话（messages 含 system+历史+本次 user）。就地追加消息，返回最终回答。"""
    for _ in range(max_steps):
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
        for tc in tool_calls:
            _args = ", ".join(f"{k}={v}" for k, v in (tc["arguments"] or {}).items())
            narrate(f"\033[36m⏺\033[0m {tc['name']}({_args})")
            result = dispatch(tc["name"], tc["arguments"], ctx)
            first = result.splitlines()[0] if result else ""
            narrate(f"\033[2m  ⎿ {first}\033[0m")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    return "（已达到本轮工具调用步数上限，请补充指令或分步进行。）"


def run_turn_stream(provider: LLMProvider, ctx: ToolContext, messages: list,
                    max_steps: int = 20, narrate: Callable[[str], None] = print,
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

    for _ in range(max_steps):
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
        for tc in tool_calls:
            _args = ", ".join(f"{k}={v}" for k, v in (tc["arguments"] or {}).items())
            narrate(f"\033[36m⏺\033[0m {tc['name']}({_args})")
            result = dispatch(tc["name"], tc["arguments"], ctx)
            first = result.splitlines()[0] if result else ""
            narrate(f"\033[2m  ⎿ {first}\033[0m")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    return {"text": "（已达到本轮工具调用步数上限，请补充指令或分步进行。）", "usage": total_usage}
