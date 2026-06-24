"""Minimal stdio MCP server exposing IvyeaAgent read-only capabilities.

This deliberately avoids the official MCP SDK so the package can keep Python
3.9 support and a small install footprint. The server is intended for local
clients such as IvyeaOps, Claude Desktop, Codex-like shells, or other agents.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, TextIO

from . import __version__, knowledge, retrieval, service, task_runner


JsonDict = dict[str, Any]


def _schema(properties: JsonDict | None = None, required: list[str] | None = None) -> JsonDict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


TOOL_DEFS: dict[str, dict[str, Any]] = {
    "ivyea_health": {
        "description": "Return IvyeaAgent version, model, knowledge and retrieval status.",
        "inputSchema": _schema(),
    },
    "ivyea_manifest": {
        "description": "Return the IvyeaAgent local integration manifest.",
        "inputSchema": _schema(),
    },
    "ivyea_knowledge_search": {
        "description": "Search bundled and user Amazon operations knowledge cards.",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }, ["query"]),
    },
    "ivyea_knowledge_audit": {
        "description": "Audit knowledge card source quality, freshness, license and conflicts.",
        "inputSchema": _schema(),
    },
    "ivyea_retrieval_search": {
        "description": "Search local knowledge, memory and persistent retrieval index.",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "sources": {"type": "array", "items": {"type": "string", "enum": ["knowledge", "memory"]}},
        }, ["query"]),
    },
    "ivyea_skill_search": {
        "description": "Search active Ivyea built-in and user skills.",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }, ["query"]),
    },
    "ivyea_system_doctor": {
        "description": "Run install/runtime doctor checks without exposing secrets.",
        "inputSchema": _schema(),
    },
    "ivyea_task_list": {
        "description": "List local long-running agent tasks and their current status.",
        "inputSchema": _schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "status": {
                "type": "string",
                "enum": ["", "pending", "in_progress", "blocked", "completed", "cancelled"],
            },
        }),
    },
    "ivyea_task_detail": {
        "description": "Load one local agent task with steps and recent events.",
        "inputSchema": _schema({
            "id": {"type": "string"},
        }, ["id"]),
    },
    "ivyea_task_resume": {
        "description": "Return the read-only resume prompt/next step for one local agent task.",
        "inputSchema": _schema({
            "id": {"type": "string"},
        }, ["id"]),
    },
    "ivyea_trace_list": {
        "description": "List recent local agent timeline events and tool calls.",
        "inputSchema": _schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "session_id": {"type": "string"},
        }),
    },
    "ivyea_trace_stats": {
        "description": "Summarize recent local agent timeline/tool-call statistics.",
        "inputSchema": _schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
        }),
    },
    "ivyea_workspace_search": {
        "description": "Read-only project search over indexed files and symbols.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 80},
        }, ["query"]),
    },
    "ivyea_workspace_inspect": {
        "description": "Read-only project map, entrypoints, tests and risk summary.",
        "inputSchema": _schema({
            "root": {"type": "string"},
        }),
    },
    "ivyea_code_plan": {
        "description": "Build a deterministic read-only code task plan.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "goal": {"type": "string"},
        }, ["goal"]),
    },
    "ivyea_code_context": {
        "description": "Collect compact read-only code context for a task.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "goal": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, ["goal"]),
    },
    "ivyea_code_bundle": {
        "description": "Build a read-only multi-round code task bundle.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "goal": {"type": "string"},
            "test_output": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, ["goal"]),
    },
    "ivyea_code_repair": {
        "description": "Parse test output and generate a read-only repair plan.",
        "inputSchema": _schema({
            "root": {"type": "string"},
            "output": {"type": "string"},
        }, ["output"]),
    },
}


def list_tools() -> list[JsonDict]:
    return [{"name": name, **spec} for name, spec in TOOL_DEFS.items()]


def call_tool(name: str, arguments: JsonDict | None = None) -> JsonDict:
    args = arguments or {}
    dispatch: dict[str, Callable[[JsonDict], JsonDict]] = {
        "ivyea_health": lambda _: service.health(),
        "ivyea_manifest": lambda _: service.manifest(),
        "ivyea_knowledge_search": lambda p: {
            "ok": True,
            "results": knowledge.search(str(p.get("query") or ""), limit=_int(p.get("limit"), 5)),
        },
        "ivyea_knowledge_audit": lambda _: service.knowledge_audit(),
        "ivyea_retrieval_search": lambda p: {
            "ok": True,
            **retrieval.search(
                str(p.get("query") or ""),
                limit=_int(p.get("limit"), 8),
                sources=p.get("sources") if isinstance(p.get("sources"), list) else None,
            ),
        },
        "ivyea_skill_search": lambda p: service.skill_search(str(p.get("query") or ""), limit=_int(p.get("limit"), 8)),
        "ivyea_system_doctor": lambda _: service.system_doctor(),
        "ivyea_task_list": lambda p: service.task_list(
            limit=_int(p.get("limit"), 20),
            status=str(p.get("status") or ""),
        ),
        "ivyea_task_detail": lambda p: service.task_detail(str(p.get("id") or "")),
        "ivyea_task_resume": lambda p: _task_resume(str(p.get("id") or "")),
        "ivyea_trace_list": lambda p: service.trace_list(
            limit=_int(p.get("limit"), 50),
            session_id=str(p.get("session_id") or ""),
        ),
        "ivyea_trace_stats": lambda p: service.trace_stats(limit=_int(p.get("limit"), 1000)),
        "ivyea_workspace_search": service.workspace_search,
        "ivyea_workspace_inspect": service.workspace_inspect,
        "ivyea_code_plan": service.code_plan,
        "ivyea_code_context": service.code_context,
        "ivyea_code_bundle": service.code_bundle,
        "ivyea_code_repair": service.code_repair,
    }
    fn = dispatch.get(name)
    if not fn:
        return _tool_result({"ok": False, "error": f"unknown tool: {name}"}, is_error=True)
    try:
        data = fn(args)
    except Exception as exc:  # noqa: BLE001 - MCP tool calls must report errors as data.
        data = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return _tool_result(data, is_error=True)
    return _tool_result(data, is_error=not bool(data.get("ok", True)))


def self_config() -> JsonDict:
    return {
        "transport": "stdio",
        "command": "ivyea",
        "args": ["mcp", "serve"],
        "note": "Read-only IvyeaAgent MCP server. Write operations are not exposed.",
    }


def _task_resume(task_id: str) -> JsonDict:
    task = task_runner.load(task_id)
    return {
        "ok": True,
        "task_id": task_id,
        "resume": task_runner.render_resume(task),
        "progress": task_runner.progress(task),
        "next_step": task_runner.next_step(task) or {},
    }


def handle_message(message: JsonDict) -> JsonDict | None:
    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if msg_id is None:
        return None
    try:
        if method == "initialize":
            return _response(msg_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ivyea-agent", "version": __version__},
            })
        if method == "ping":
            return _response(msg_id, {})
        if method == "tools/list":
            return _response(msg_id, {"tools": list_tools()})
        if method == "tools/call":
            return _response(msg_id, call_tool(str(params.get("name") or ""), params.get("arguments") or {}))
        return _error(msg_id, -32601, f"Method not found: {method}")
    except Exception as exc:  # noqa: BLE001
        return _error(msg_id, -32603, f"{type(exc).__name__}: {exc}")


def serve_stdio(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for raw in stdin:
        if not raw.strip():
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = handle_message(message)
        if response is None:
            continue
        stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
        stdout.flush()
    return 0


def _tool_result(data: JsonDict, *, is_error: bool = False) -> JsonDict:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > 12_000:
        text = text[:11_900] + "\n...truncated"
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": data,
        "isError": is_error,
    }


def _response(msg_id: Any, result: JsonDict) -> JsonDict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> JsonDict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
