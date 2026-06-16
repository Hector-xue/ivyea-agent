"""对话式 Agent 循环（ReAct 工具调用）。

provider.chat(messages, tools) → 若有 tool_calls 则逐个派发(写工具内部走人工
审批)、把结果回灌 → 直到模型给出最终回答。带步数上限防失控。
"""
from __future__ import annotations

import json
from typing import Callable

from .agent_tools import TOOL_SCHEMAS, ToolContext, dispatch
from .providers import LLMProvider

SYSTEM_PROMPT = """你是 Ivyea Agent，一个亚马逊运营助手（当前专长：广告巡检）。
通过调用工具完成任务：run_patrol(巡检) → propose_actions(看可执行动作) → execute_actions(执行，会逐条弹人工审批) → 必要时 rollback。
原则：先巡检拿证据再提动作；写操作一律经人工审批，绝不自作主张直接写；建议要绑数据、简洁可执行；信息不足就向用户澄清，不要瞎编 ASIN 或规格。"""


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
            narrate(f"  🔧 {tc['name']}({', '.join(f'{k}={v}' for k, v in (tc['arguments'] or {}).items())})")
            result = dispatch(tc["name"], tc["arguments"], ctx)
            narrate(f"     ↳ {result.splitlines()[0] if result else ''}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    return "（已达到本轮工具调用步数上限，请补充指令或分步进行。）"
