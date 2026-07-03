"""stream-json 事件构造（`ivyea chat -p --output-format stream-json`）。

逐行 NDJSON 打到 stdout，事件字段名对齐 Claude Code 的 stream-json 输出
（system/init → assistant → user/tool_result → result），让 IvyeaOps 等已解析
Claude Code 格式的消费方可以复用同一套渲染。唯一有意差异：计价单位是人民币，
用 total_cost_cny 而不伪装成 total_cost_usd。
"""
from __future__ import annotations

import json


def emit_line(ev: dict) -> None:
    """一行一个 JSON 事件到 stdout（NDJSON；消费端按行 json.loads）。"""
    print(json.dumps(ev, ensure_ascii=False), flush=True)


def init_event(session_id: str, model: str, cwd: str, tools: list,
               permission_mode: str = "default") -> dict:
    """会话起始事件：声明 session_id/模型/工具面，供消费方建立上下文。"""
    return {
        "type": "system", "subtype": "init", "session_id": session_id,
        "cwd": cwd, "model": model, "tools": list(tools),
        "permissionMode": permission_mode, "apiKeySource": "user",
    }


def assistant_event(session_id: str, text: str, tool_calls: list) -> dict:
    """一个模型步一条：content 块顺序为 text（若有）→ 各 tool_use（对齐 Anthropic message 形状）。"""
    content: list = []
    if text:
        content.append({"type": "text", "text": text})
    for tc in tool_calls or []:
        content.append({"type": "tool_use", "id": tc.get("id", ""),
                        "name": tc.get("name", ""), "input": tc.get("arguments") or {}})
    return {"type": "assistant", "session_id": session_id,
            "message": {"role": "assistant", "content": content}}


def tool_result_event(session_id: str, tool_use_id: str, text: str, is_error: bool) -> dict:
    """工具结果事件：tool_use_id 与 assistant 事件里的 tool_use.id 配对。"""
    return {"type": "user", "session_id": session_id,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id,
                 "content": text, "is_error": bool(is_error)}]}}


def result_event(session_id: str, text: str, usage: dict, cost_cny: float,
                 duration_ms: int, num_turns: int = 1, is_error: bool = False) -> dict:
    """收尾事件：最终答案 + 用量/花费汇总。is_error 覆盖 blocked/异常收尾。"""
    usage = usage or {}
    return {
        "type": "result",
        "subtype": "success" if not is_error else "error_during_execution",
        "is_error": bool(is_error), "result": text, "session_id": session_id,
        "duration_ms": int(duration_ms), "num_turns": int(num_turns),
        "total_cost_cny": round(float(cost_cny or 0.0), 6),
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "cache_read_input_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
        },
    }
