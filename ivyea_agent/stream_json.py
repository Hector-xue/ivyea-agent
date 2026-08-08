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


# 一次工具调用的参数里，值得放进事件的键。整包参数可能很大（文件内容、报表行），
# 事件是给 UI 画芯片用的，只带够画一行摘要的量。
_ARG_KEEP = (
    "query", "q", "keyword", "asin", "pattern", "path", "url", "command",
    "name", "tool", "server", "marketplace", "country", "site", "mode", "days",
    "project_id", "job_id", "sid", "skill_name", "task", "limit",
)
_ARG_VALUE_MAX = 200


def _slim_args(args: dict | None) -> dict:
    """裁剪工具参数：只留可读的标量键，长值截断。"""
    out: dict = {}
    for key in _ARG_KEEP:
        val = (args or {}).get(key)
        if val is None or isinstance(val, (dict, list)):
            continue
        text = str(val)
        if not text:
            continue
        out[key] = text[:_ARG_VALUE_MAX]
    return out


# 单条 diff 的上限。写一个几万行的文件时，整段 diff 塞进 SSE 会把一轮的事件流
# 撑到几 MB —— 浏览器要全收下来才画得出那一格，代价不成比例。
_DIFF_MAX = 6000


def file_change_event(session_id: str, turn_id: str, path: str, action: str,
                      diff: str, scope: str = "file") -> dict:
    """Agent 改过一个文件。

    step 事件里虽然有 `path`，但它只说明"调用了 write_file"，说不出**改了什么**。
    这个事件带上 diff，UI 才画得出 diff 那一格。

    `scope` 区分两种粒度，界面上不能混为一谈：
      - "file"     write_file：整文件的前后对比
      - "fragment" edit_file：只有被替换的那一段，行号是片段内的相对行号
    """
    body = diff or ""
    truncated = len(body) > _DIFF_MAX
    return {
        "type": "file_change",
        "session_id": session_id,
        "turn_id": turn_id,
        "path": path,
        "action": action,          # create / overwrite / edit
        "scope": scope,
        "diff": body[:_DIFF_MAX],
        "truncated": truncated,
    }


def step_event(session_id: str, turn_id: str, call_id: str, seq: int, name: str,
               arguments: dict | None, status: str, duration_ms: int | None = None) -> dict:
    """一次工具调用的步骤事件（开始 running，收尾 ok/error）。

    stream-json 的 assistant/tool_result 事件足够回放对话，但不足以画出参考产品
    那种「中文工具名 · ✓ · 5.2s」的执行时间线：它既没有耗时，也没有把真正被调用
    的东西说清楚 —— agent 不把 MCP / 板块工具扁平化进工具名空间，真实工具藏在
    参数里（`mcp_call_tool(server=…, tool=…)`、`ivyea_ops_call_tool(name=…)`）。
    这里统一拆包提到顶层，消费方不必再去猜参数结构。

    耗时本来只进 traces.db；同时放进事件流，UI 就不用为了显示一个秒数去 join 另一张表。
    """
    args = arguments or {}
    phase = "tool"
    tool = ""
    server = ""
    if name == "mcp_call_tool":
        phase = "mcp"
        server = str(args.get("server") or "")
        tool = str(args.get("tool") or "")
    elif name == "ivyea_ops_call_tool":
        phase = "board"
        tool = str(args.get("name") or "")
    elif name == "dispatch_subagent":
        phase = "subagent"
    elif name in ("knowledge_search", "recall", "skill_search"):
        phase = "knowledge"
    elif name in ("todo_write", "progress_update", "self_critique"):
        # 规划/汇报类调用在一轮里能占到大多数步（实测 19 步里 12 步是它们）。
        # 它们不是"做了什么"，是"在组织怎么做"。单独归一类，UI 可以折成一行，
        # 免得真正干活的那两三步被埋掉。
        phase = "plan"

    # MCP / 板块工具的真实入参在嵌套的 arguments 里，摘要要看那一层。
    inner = args.get("arguments") if isinstance(args.get("arguments"), dict) else None
    ev = {
        "type": "step", "session_id": session_id, "turn_id": turn_id,
        "id": call_id, "seq": int(seq), "phase": phase, "name": name,
        "status": status, "args": _slim_args(inner if inner is not None else args),
    }
    if tool:
        ev["tool"] = tool
    if server:
        ev["server"] = server
    if duration_ms is not None:
        ev["ms"] = int(duration_ms)
    return ev


def skill_match_event(session_id: str, skills: list) -> dict:
    """本轮命中的 skill —— 对应参考产品的「✓ 理解问题，匹配最合适的技能」。

    skills 里每项形如 {id, title, domain, score}。
    """
    return {"type": "skill_match", "session_id": session_id, "skills": list(skills or [])}


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
