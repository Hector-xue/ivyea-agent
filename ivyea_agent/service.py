"""Local HTTP API for embedding IvyeaAgent in IvyeaOps."""
from __future__ import annotations

import json
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import (
    __version__, agent_loop, code_agent, config, knowledge, models,
    retrieval, security, self_manage, sessions, skills, task_runner,
    traces, workspace,
)
from .agent_tools import ToolContext


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def health() -> dict[str, Any]:
    model_cfg = config.get_model_config()
    return {
        "ok": True,
        "name": "ivyea-agent",
        "version": __version__,
        "data_dir": str(config.IVYEA_DIR),
        "model": {
            "provider": model_cfg.get("provider", ""),
            "label": model_cfg.get("label", ""),
            "model": model_cfg.get("model", ""),
            "api_mode": model_cfg.get("api_mode", ""),
            "auth_type": model_cfg.get("auth_type", ""),
            "key_status": models.key_status(models.provider_by_id(model_cfg.get("provider", "")) or model_cfg),
        },
        "knowledge": {
            "cards": len(knowledge.list_cards()),
            "user_cards": len(knowledge.list_user_cards()),
        },
        "retrieval": retrieval.capabilities(),
    }


def manifest() -> dict[str, Any]:
    return {
        "ok": True,
        "name": "ivyea-agent",
        "version": __version__,
        "api_version": "v1",
        "default_base_url": f"http://{DEFAULT_HOST}:{DEFAULT_PORT}",
        "security": {
            "default_bind": DEFAULT_HOST,
            "remote_bind_requires": "--allow-remote plus API token",
            "auth": {
                "type": "bearer",
                "env": "IVYEA_API_TOKEN",
                "required_for_remote_bind": True,
                "local_default_required": False,
            },
            "secrets_in_responses": False,
        },
        "capabilities": {
            "health": True,
            "knowledge_search": True,
            "knowledge_management": True,
            "local_retrieval": retrieval.capabilities(),
            "task_state": True,
            "chat": True,
            "workspace_understanding": True,
            "code_agent": True,
            "mcp_stdio_server": True,
            "write_execution": False,
        },
        "mcp": {
            "transport": "stdio",
            "command": "ivyea",
            "args": ["mcp", "serve"],
            "read_only": True,
        },
        "endpoints": [
            {"method": "GET", "path": "/health", "description": "health, version, model status, knowledge and retrieval summary"},
            {"method": "GET", "path": "/v1/manifest", "description": "IvyeaOps integration manifest"},
            {"method": "GET", "path": "/v1/openapi.json", "description": "OpenAPI discovery document"},
            {"method": "GET", "path": "/v1/capabilities", "description": "retrieval capabilities"},
            {"method": "GET", "path": "/v1/model", "description": "current model status without secrets"},
            {"method": "GET", "path": "/v1/mcp/self-config", "description": "stdio MCP server config for local clients"},
            {"method": "GET", "path": "/v1/system/status", "description": "install/runtime status for IvyeaOps diagnostics"},
            {"method": "GET", "path": "/v1/system/doctor", "description": "install/runtime doctor checks"},
            {"method": "GET", "path": "/v1/chat/sessions", "description": "list persisted embedded chat sessions"},
            {"method": "POST", "path": "/v1/chat/sessions", "description": "create an embedded chat session"},
            {"method": "GET", "path": "/v1/chat/sessions/{id}", "description": "load embedded chat session"},
            {"method": "POST", "path": "/v1/chat", "description": "run one read-only embedded agent turn"},
            {"method": "POST", "path": "/v1/chat/stream", "description": "run one read-only embedded agent turn as server-sent events"},
            {"method": "GET", "path": "/v1/skills", "description": "list active built-in and user skills"},
            {"method": "GET", "path": "/v1/skills/search", "description": "search active skills"},
            {"method": "GET", "path": "/v1/skills/{id}", "description": "load skill detail"},
            {"method": "GET", "path": "/v1/knowledge/cards", "description": "list bundled and user knowledge cards"},
            {"method": "POST", "path": "/v1/knowledge/cards", "description": "create a user-supplied knowledge card"},
            {"method": "GET", "path": "/v1/knowledge/cards/{id}", "description": "load knowledge card detail"},
            {"method": "GET", "path": "/v1/knowledge/audit", "description": "structured source quality and freshness audit"},
            {"method": "GET", "path": "/v1/knowledge/conflicts", "description": "knowledge conflict review queue"},
            {"method": "POST", "path": "/v1/knowledge/rebuild", "description": "validate knowledge metadata and rebuild local indexes"},
            {"method": "GET", "path": "/v1/knowledge/search", "description": "query bundled and user knowledge"},
            {"method": "GET", "path": "/v1/retrieval/embeddings", "description": "local embedding backend status"},
            {"method": "GET", "path": "/v1/retrieval/status", "description": "persistent local retrieval index status"},
            {"method": "POST", "path": "/v1/retrieval/search", "description": "unified local retrieval over knowledge and memory"},
            {"method": "POST", "path": "/v1/retrieval/embeddings", "description": "configure local embedding backend"},
            {"method": "POST", "path": "/v1/retrieval/embeddings/probe", "description": "probe configured local embedding backend"},
            {"method": "POST", "path": "/v1/retrieval/index", "description": "rebuild persistent local retrieval index"},
            {"method": "GET", "path": "/v1/tasks", "description": "list tasks"},
            {"method": "POST", "path": "/v1/tasks", "description": "create task"},
            {"method": "GET", "path": "/v1/tasks/{id}", "description": "load task detail"},
            {"method": "POST", "path": "/v1/tasks/{id}/start", "description": "start next task step"},
            {"method": "POST", "path": "/v1/tasks/{id}/step", "description": "update a task step"},
            {"method": "POST", "path": "/v1/tasks/{id}/status", "description": "update task status"},
            {"method": "POST", "path": "/v1/tasks/{id}/log", "description": "append task log"},
            {"method": "GET", "path": "/v1/traces", "description": "recent local agent timeline events"},
            {"method": "GET", "path": "/v1/traces/stats", "description": "local agent timeline statistics"},
            {"method": "POST", "path": "/v1/workspace/index", "description": "build and persist a read-only workspace index"},
            {"method": "POST", "path": "/v1/workspace/search", "description": "search indexed project files and symbols"},
            {"method": "POST", "path": "/v1/workspace/inspect", "description": "summarize project entrypoints, tests, and risks"},
            {"method": "POST", "path": "/v1/workspace/symbols", "description": "search workspace symbols"},
            {"method": "POST", "path": "/v1/workspace/impact", "description": "analyze symbol/file impact"},
            {"method": "POST", "path": "/v1/code/plan", "description": "build a deterministic code task plan"},
            {"method": "POST", "path": "/v1/code/context", "description": "collect compact code context for a task"},
            {"method": "POST", "path": "/v1/code/quality", "description": "run read-only code quality heuristics"},
            {"method": "POST", "path": "/v1/code/review", "description": "run read-only diff review gate"},
            {"method": "POST", "path": "/v1/code/repair", "description": "parse test output and generate a repair plan"},
        ],
    }


def openapi_spec() -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for endpoint in manifest()["endpoints"]:
        path = endpoint["path"]
        method = endpoint["method"].lower()
        paths.setdefault(path, {})[method] = {
            "summary": endpoint.get("description", ""),
            "operationId": _operation_id(method, path),
            "responses": {
                "200": {
                    "description": "JSON response",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                }
            },
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Ivyea Agent Local API", "version": __version__},
        "servers": [{"url": f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
            }
        },
        "paths": paths,
    }


def task_list(limit: int = 20, status: str = "") -> dict[str, Any]:
    return {"ok": True, "tasks": task_runner.list_tasks(limit=limit, status=status or "")}


def mcp_self_config() -> dict[str, Any]:
    return {
        "ok": True,
        "mcp": {
            "transport": "stdio",
            "command": "ivyea",
            "args": ["mcp", "serve"],
            "read_only": True,
            "note": "Read-only IvyeaAgent MCP server. Write operations are not exposed.",
        },
    }


def task_detail(task_id: str) -> dict[str, Any]:
    return {"ok": True, "task": task_runner.load(task_id)}


def task_create(payload: dict[str, Any]) -> dict[str, Any]:
    steps = payload.get("steps")
    if isinstance(steps, str):
        steps = [s.strip() for s in steps.split("|") if s.strip()]
    if not isinstance(steps, list):
        steps = []
    task = task_runner.create(
        str(payload.get("title") or ""),
        steps=[str(s) for s in steps],
        notes=str(payload.get("notes") or ""),
        workspace=str(payload.get("workspace") or ""),
    )
    return {"ok": True, "task": task}


def task_update(task_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    note = str(payload.get("notes") or payload.get("note") or "")
    if action == "start":
        task = task_runner.start_next(task_id, note=note)
    elif action == "step":
        task = task_runner.update_step(
            task_id,
            _int(payload.get("index"), 1),
            str(payload.get("status") or ""),
            note=note,
        )
    elif action == "status":
        task = task_runner.set_status(task_id, str(payload.get("status") or ""), note=note)
    elif action == "log":
        task = task_runner.append_log(task_id, str(payload.get("text") or note), kind=str(payload.get("kind") or "log"))
    else:
        raise ValueError(f"unknown task action: {action}")
    return {"ok": True, "task": task}


def trace_list(limit: int = 50, session_id: str = "") -> dict[str, Any]:
    return {"ok": True, "traces": [_public_trace(row) for row in traces.recent(limit=limit, session_id=session_id or "")]}


def trace_stats(limit: int = 1000) -> dict[str, Any]:
    return {"ok": True, "stats": traces.stats(limit=limit)}


def system_status() -> dict[str, Any]:
    return {"ok": True, "status": _public_install_info(self_manage.install_info())}


def system_doctor() -> dict[str, Any]:
    data = self_manage.install_doctor()
    return {
        "ok": bool(data.get("ok")),
        "info": _public_install_info(data.get("info") or {}),
        "checks": data.get("checks") or [],
        "next_steps": data.get("next_steps") or [],
    }


def skill_list(limit: int = 100) -> dict[str, Any]:
    rows = skills.list_skills()[:max(1, min(int(limit or 100), 500))]
    return {"ok": True, "skills": [_public_skill(sk) for sk in rows]}


def skill_search(query: str, limit: int = 8) -> dict[str, Any]:
    hits = skills.search(query, limit=max(1, min(int(limit or 8), 50)))
    return {"ok": True, "query": query, "skills": [{**_public_skill(sk), "score": score} for sk, score in hits]}


def skill_detail(skill_id: str) -> dict[str, Any]:
    sk = skills.get_skill(skill_id)
    if not sk:
        raise FileNotFoundError(f"skill 不存在：{skill_id}")
    return {"ok": True, "skill": _public_skill(sk, include_body=True)}


def knowledge_cards(limit: int = 200) -> dict[str, Any]:
    rows = knowledge.list_cards()[:max(1, min(int(limit or 200), 1000))]
    return {"ok": True, "cards": [_public_knowledge_card(card) for card in rows]}


def knowledge_detail(card_id: str) -> dict[str, Any]:
    card = knowledge.get_card(card_id)
    if not card:
        raise FileNotFoundError(f"知识卡不存在：{card_id}")
    return {"ok": True, "card": _public_knowledge_card(card, include_body=True)}


def knowledge_create(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or payload.get("id") or "用户知识").strip()
    body = str(payload.get("body") or payload.get("content") or "").strip()
    if not body:
        raise ValueError("body is required")
    tags = payload.get("tags")
    if isinstance(tags, str):
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        tag_list = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tag_list = []
    card = knowledge.import_text(
        title,
        body,
        source_url=str(payload.get("source_url") or ""),
        source_type=str(payload.get("source_type") or "user"),
        confidence=str(payload.get("confidence") or ""),
        tags=tag_list,
        card_id=str(payload.get("id") or ""),
        license=str(payload.get("license") or "user_supplied"),
    )
    indexes: dict[str, Any] = {}
    if payload.get("rebuild", True):
        indexes["knowledge"] = knowledge.rebuild_index()
        indexes["retrieval"] = retrieval.rebuild_index()
    return {"ok": True, "card": _public_knowledge_card({**card, "body": body}, include_body=True), "indexes": indexes}


def knowledge_audit() -> dict[str, Any]:
    data = knowledge.audit()
    return {
        "ok": True,
        "summary": data.get("summary") or {},
        "cards": [_public_knowledge_audit(card) for card in data.get("cards") or []],
        "conflicts": data.get("conflicts") or [],
    }


def knowledge_conflicts() -> dict[str, Any]:
    return {"ok": True, "conflicts": knowledge.conflicts()}


def knowledge_rebuild() -> dict[str, Any]:
    data = knowledge.rebuild()
    data["retrieval_index"] = retrieval.rebuild_index()
    return {"ok": True, **data}


def workspace_index(payload: dict[str, Any]) -> dict[str, Any]:
    options = workspace.ScanOptions(
        max_files=max(1, min(_int(payload.get("max_files"), 2000), 10000)),
        max_bytes=max(1024, min(_int(payload.get("max_bytes"), 256_000), 2_000_000)),
        include_hidden=bool(payload.get("include_hidden", False)),
    )
    idx = workspace.build_index(_root(payload), options)
    path = workspace.save_index(idx)
    return {"ok": True, "workspace": _public_workspace_index(idx, path)}


def workspace_search(payload: dict[str, Any]) -> dict[str, Any]:
    rows = workspace.search(str(payload.get("query") or ""), root=_root(payload), limit=_int(payload.get("limit"), 10))
    return {"ok": True, "root": str(workspace.resolve_root(_root(payload))), "results": rows}


def workspace_inspect(payload: dict[str, Any]) -> dict[str, Any]:
    root = _root(payload)
    return {"ok": True, "map": workspace.project_map(root), "inspect": workspace.project_inspect(root)}


def workspace_symbols(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **workspace.symbol_index(_root(payload), query=str(payload.get("query") or ""), limit=_int(payload.get("limit"), 80))}


def workspace_impact(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **workspace.impact_analysis(str(payload.get("target") or payload.get("query") or ""), _root(payload), limit=_int(payload.get("limit"), 80))}


def code_plan(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "plan": code_agent.task_plan(str(payload.get("goal") or ""), root=_root(payload))}


def code_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "context": code_agent.context(
            str(payload.get("goal") or ""),
            root=_root(payload),
            limit=_int(payload.get("limit"), 8),
        ),
    }


def code_quality(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "quality": code_agent.quality(root=_root(payload))}


def code_review(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "review": code_agent.review_ready(root=_root(payload), staged=bool(payload.get("staged", False)))}


def code_repair(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "repair": code_agent.repair_plan(str(payload.get("output") or payload.get("text") or ""), root=_root(payload))}


def chat_run(payload: dict[str, Any], provider: Any | None = None) -> dict[str, Any]:
    """Run one embedded agent turn for IvyeaOps.

    The HTTP service defaults to read-only plan mode so write/execute tools do
    not prompt inside a headless API request. IvyeaOps can still show suggested
    actions, then route approved writes through explicit product UI flows.
    """
    from .providers import LLMError, build_chain

    message = str(payload.get("message") or payload.get("input") or "").strip()
    if not message:
        raise ValueError("message is required")
    model_cfg = config.get_model_config()
    api_key = config.get_active_key()
    if _model_requires_key(model_cfg) and not api_key and provider is None:
        return {"ok": False, "error": "model_not_configured", "model": health()["model"]}

    plan_mode = payload.get("plan_mode")
    if plan_mode is None:
        plan_mode = True
    ctx = ToolContext(
        execute=False,
        plan_mode=bool(plan_mode),
        workspace=str(payload.get("workspace") or ""),
        task_id=str(payload.get("task_id") or ""),
    )
    ctx.session_id = str(payload.get("session_id") or "") or sessions.new_id()
    ctx.turn_id = str(payload.get("turn_id") or "")
    if payload.get("asin"):
        ctx.asin = str(payload.get("asin") or "")

    messages, created_at = _chat_messages(message, payload, ctx)
    events: list[dict[str, Any]] = []

    def narrate(text: str) -> None:
        events.append({"type": "event", "text": security.redact_text(str(text))})

    try:
        provider = provider or build_chain(model_cfg, api_key, narrate=narrate)
        text = agent_loop.run_turn(provider, ctx, messages, max_steps=_int(payload.get("max_steps"), 12), narrate=narrate)
    except LLMError as exc:
        return {"ok": False, "error": "model_error", "detail": str(exc), "events": events}

    result: dict[str, Any] = {
        "ok": True,
        "session_id": ctx.session_id,
        "text": text,
        "events": events,
        "messages": _public_messages(messages),
        "model": health()["model"],
        "read_only": bool(plan_mode),
        "todos": list(ctx.todos or []),
    }
    if ctx.task_id:
        try:
            result["task"] = task_runner.load(ctx.task_id)
        except (FileNotFoundError, OSError, ValueError):
            pass
    if payload.get("persist", True):
        sessions.save(
            ctx.session_id,
            messages,
            model=model_cfg.get("model", ""),
            usage={},
            created=created_at,
        )
    return result


def chat_stream(payload: dict[str, Any], send: Any, provider: Any | None = None) -> dict[str, Any]:
    """Run one embedded agent turn and emit SSE-style events through send(event, data)."""
    from .providers import LLMError, build_chain

    message = str(payload.get("message") or payload.get("input") or "").strip()
    if not message:
        data = {"ok": False, "error": "message is required"}
        send("error", data)
        return data
    model_cfg = config.get_model_config()
    api_key = config.get_active_key()
    if _model_requires_key(model_cfg) and not api_key and provider is None:
        data = {"ok": False, "error": "model_not_configured", "model": health()["model"]}
        send("error", data)
        return data

    plan_mode = payload.get("plan_mode")
    if plan_mode is None:
        plan_mode = True
    ctx = ToolContext(
        execute=False,
        plan_mode=bool(plan_mode),
        workspace=str(payload.get("workspace") or ""),
        task_id=str(payload.get("task_id") or ""),
    )
    ctx.session_id = str(payload.get("session_id") or "") or sessions.new_id()
    ctx.turn_id = str(payload.get("turn_id") or "")
    if payload.get("asin"):
        ctx.asin = str(payload.get("asin") or "")

    messages, created_at = _chat_messages(message, payload, ctx)
    send("start", {"ok": True, "session_id": ctx.session_id, "read_only": bool(plan_mode), "model": health()["model"]})

    def narrate(text: str) -> None:
        send("event", {"type": "event", "text": security.redact_text(str(text))})

    try:
        provider = provider or build_chain(model_cfg, api_key, narrate=narrate)
        out = agent_loop.run_turn_stream(
            provider,
            ctx,
            messages,
            max_steps=_int(payload.get("max_steps"), 12),
            narrate=narrate,
            render=lambda text: send("token", {"text": security.redact_text(str(text))}),
            model=model_cfg.get("model", ""),
        )
    except LLMError as exc:
        data = {"ok": False, "error": "model_error", "detail": str(exc)}
        send("error", data)
        return data

    if payload.get("persist", True):
        sessions.save(ctx.session_id, messages, model=model_cfg.get("model", ""), usage={}, created=created_at)
    data = {
        "ok": True,
        "session_id": ctx.session_id,
        "text": out.get("text", ""),
        "usage": out.get("usage") or {},
        "messages": _public_messages(messages),
        "read_only": bool(plan_mode),
        "todos": list(ctx.todos or []),
    }
    send("final", data)
    return data


def chat_session_list(limit: int = 20) -> dict[str, Any]:
    return {"ok": True, "sessions": [_public_session(row) for row in sessions.listing(limit=limit)]}


def chat_session_detail(session_id: str) -> dict[str, Any]:
    data = sessions.load(session_id)
    if not data:
        raise FileNotFoundError(f"会话不存在：{session_id}")
    return {"ok": True, "session": _public_session_detail(data)}


def chat_session_create(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("id") or "") or sessions.new_id()
    initial = str(payload.get("message") or payload.get("title") or "").strip()
    messages: list[dict[str, Any]] = []
    if initial:
        messages.append({"role": "user", "content": initial})
    sessions.save(session_id, messages, model=config.get_model_config().get("model", ""))
    data = sessions.load(session_id) or {"id": session_id, "messages": messages}
    return {"ok": True, "session": _public_session_detail(data)}


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, api_token: str = "") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, int(port)), _Handler)
    server.api_token = api_token or ""  # type: ignore[attr-defined]
    return server


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, api_token: str = "") -> None:
    server = make_server(host, port, api_token=api_token)
    actual_host, actual_port = server.server_address
    print(f"Ivyea Agent API listening on http://{actual_host}:{actual_port}")
    if api_token:
        print("Auth: Bearer token required.")
    print("Endpoints: /health, /v1/manifest, /v1/capabilities, /v1/knowledge/search, /v1/retrieval/search, /v1/tasks")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nIvyea Agent API stopped.")
    finally:
        server.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "IvyeaAgentHTTP/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path in ("/health", "/v1/health"):
            self._json(200, health())
            return
        if parsed.path == "/v1/manifest":
            self._json(200, manifest())
            return
        if parsed.path == "/v1/openapi.json":
            self._json(200, openapi_spec())
            return
        if parsed.path == "/v1/capabilities":
            self._json(200, {"ok": True, "retrieval": retrieval.capabilities()})
            return
        if parsed.path == "/v1/model":
            self._json(200, {"ok": True, "model": health()["model"]})
            return
        if parsed.path == "/v1/mcp/self-config":
            self._json(200, mcp_self_config())
            return
        if parsed.path == "/v1/system/status":
            self._json(200, system_status())
            return
        if parsed.path == "/v1/system/doctor":
            self._json(200, system_doctor())
            return
        if parsed.path == "/v1/chat/sessions":
            self._json(200, chat_session_list(limit=_int(_first(qs, "limit"), 20)))
            return
        if parsed.path.startswith("/v1/chat/sessions/"):
            session_id = parsed.path.rsplit("/", 1)[-1]
            try:
                self._json(200, chat_session_detail(session_id))
            except FileNotFoundError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/skills":
            self._json(200, skill_list(limit=_int(_first(qs, "limit"), 100)))
            return
        if parsed.path == "/v1/skills/search":
            self._json(200, skill_search(_first(qs, "q") or _first(qs, "query"), limit=_int(_first(qs, "limit"), 8)))
            return
        if parsed.path.startswith("/v1/skills/"):
            skill_id = parsed.path.rsplit("/", 1)[-1]
            try:
                self._json(200, skill_detail(skill_id))
            except FileNotFoundError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/cards":
            self._json(200, knowledge_cards(limit=_int(_first(qs, "limit"), 200)))
            return
        if parsed.path == "/v1/knowledge/audit":
            self._json(200, knowledge_audit())
            return
        if parsed.path == "/v1/knowledge/conflicts":
            self._json(200, knowledge_conflicts())
            return
        if parsed.path.startswith("/v1/knowledge/cards/"):
            card_id = parsed.path.rsplit("/", 1)[-1]
            try:
                self._json(200, knowledge_detail(card_id))
            except FileNotFoundError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/search":
            query = _first(qs, "q") or _first(qs, "query")
            limit = _int(_first(qs, "limit"), 5)
            self._json(200, {"ok": True, "results": knowledge.search(query, limit=limit)})
            return
        if parsed.path == "/v1/retrieval/status":
            self._json(200, {"ok": True, "index": retrieval.index_status()})
            return
        if parsed.path == "/v1/retrieval/embeddings":
            self._json(200, {"ok": True, "embeddings": retrieval.embeddings_status()})
            return
        if parsed.path == "/v1/tasks":
            self._json(200, task_list(limit=_int(_first(qs, "limit"), 20), status=_first(qs, "status")))
            return
        if parsed.path == "/v1/traces":
            self._json(200, trace_list(limit=_int(_first(qs, "limit"), 50), session_id=_first(qs, "session_id") or _first(qs, "session")))
            return
        if parsed.path == "/v1/traces/stats":
            self._json(200, trace_stats(limit=_int(_first(qs, "limit"), 1000)))
            return
        if parsed.path.startswith("/v1/tasks/"):
            task_id = parsed.path.split("/", 3)[-1]
            try:
                self._json(200, task_detail(task_id))
            except FileNotFoundError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not_found", "path": parsed.path})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path == "/v1/chat/stream":
            self._sse_begin()
            chat_stream(body, self._sse_send)
            return
        if parsed.path == "/v1/chat":
            try:
                self._json(200, chat_run(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/chat/sessions":
            self._json(200, chat_session_create(body))
            return
        if parsed.path == "/v1/knowledge/cards":
            try:
                self._json(200, knowledge_create(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/rebuild":
            self._json(200, knowledge_rebuild())
            return
        if parsed.path == "/v1/retrieval/search":
            result = retrieval.search(
                str(body.get("query") or ""),
                limit=_int(body.get("limit"), 8),
                sources=body.get("sources") if isinstance(body.get("sources"), list) else None,
            )
            self._json(200, {"ok": True, **result})
            return
        if parsed.path == "/v1/retrieval/index":
            self._json(200, retrieval.rebuild_index())
            return
        if parsed.path == "/v1/retrieval/embeddings":
            model_path = body.get("model_path") if "model_path" in body else None
            data = {
                "ok": True,
                "embeddings": retrieval.configure_embeddings(
                    backend=str(body.get("backend") or ""),
                    model=str(body.get("model") or ""),
                    model_path="" if model_path is None and "model_path" in body else (
                        str(model_path) if model_path is not None else None
                    ),
                    allow_download=body.get("allow_download") if isinstance(body.get("allow_download"), bool) else None,
                ),
            }
            if body.get("probe"):
                data["probe"] = retrieval.probe_embeddings(str(body.get("probe_text") or ""))
            self._json(200, data)
            return
        if parsed.path == "/v1/retrieval/embeddings/probe":
            self._json(200, {"ok": True, "probe": retrieval.probe_embeddings(str(body.get("text") or ""))})
            return
        if parsed.path == "/v1/tasks":
            try:
                self._json(200, task_create(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/v1/tasks/"):
            parts = parsed.path.split("/")
            if len(parts) >= 5:
                task_id, action = parts[3], parts[4]
                try:
                    self._json(200, task_update(task_id, action, body))
                except FileNotFoundError as exc:
                    self._json(404, {"ok": False, "error": str(exc)})
                except (ValueError, IndexError) as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                return
        if parsed.path == "/v1/workspace/index":
            self._json(200, workspace_index(body))
            return
        if parsed.path == "/v1/workspace/search":
            self._json(200, workspace_search(body))
            return
        if parsed.path == "/v1/workspace/inspect":
            self._json(200, workspace_inspect(body))
            return
        if parsed.path == "/v1/workspace/symbols":
            self._json(200, workspace_symbols(body))
            return
        if parsed.path == "/v1/workspace/impact":
            self._json(200, workspace_impact(body))
            return
        if parsed.path == "/v1/code/plan":
            self._json(200, code_plan(body))
            return
        if parsed.path == "/v1/code/context":
            self._json(200, code_context(body))
            return
        if parsed.path == "/v1/code/quality":
            self._json(200, code_quality(body))
            return
        if parsed.path == "/v1/code/review":
            self._json(200, code_review(body))
            return
        if parsed.path == "/v1/code/repair":
            self._json(200, code_repair(body))
            return
        self._json(404, {"ok": False, "error": "not_found", "path": parsed.path})

    def _read_json(self) -> dict[str, Any]:
        length = _int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _authorized(self) -> bool:
        token = str(getattr(self.server, "api_token", "") or "")
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if hmac.compare_digest(header, f"Bearer {token}"):
            return True
        self._json(401, {"ok": False, "error": "unauthorized"})
        return False

    def _json(self, status: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse_begin(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _sse_send(self, event: str, data: dict[str, Any]) -> None:
        raw = (
            f"event: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
        ).encode("utf-8")
        self.wfile.write(raw)
        self.wfile.flush()


def _first(qs: dict[str, list[str]], key: str) -> str:
    vals = qs.get(key) or []
    return vals[0] if vals else ""


def _operation_id(method: str, path: str) -> str:
    parts = [p.strip("{}") for p in path.strip("/").split("/") if p and not p.startswith("v1")]
    clean = [part.replace("-", "_").replace(".", "_") for part in parts]
    return method + "_" + "_".join(clean or ["root"])


def _root(payload: dict[str, Any]) -> str:
    return str(payload.get("root") or payload.get("workspace") or ".")


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _model_requires_key(settings: dict[str, Any]) -> bool:
    auth = (settings.get("auth_type") or "api_key").lower()
    if auth in ("none", "aws_sdk"):
        return False
    return bool(settings.get("key_env") or auth in ("oauth_external", "oauth_device_code", "copilot"))


def _chat_messages(message: str, payload: dict[str, Any], ctx: ToolContext) -> tuple[list[dict[str, Any]], float | None]:
    system = agent_loop.SYSTEM_PROMPT
    if ctx.plan_mode:
        system += agent_loop.PLAN_NOTE
    system += "\n\n[IvyeaOps 嵌入模式] 当前默认只读。需要写入广告、文件或执行命令时，先输出计划和审批项，不要在本轮直接执行。"
    if payload.get("system"):
        system += "\n\n[调用方系统上下文]\n" + str(payload.get("system") or "")
    created_at = None
    saved = sessions.load(ctx.session_id) if ctx.session_id else None
    if saved and isinstance(saved.get("messages"), list):
        messages = list(saved.get("messages") or [])
        created_at = saved.get("created")
        if messages and messages[0].get("role") == "system":
            messages[0] = {"role": "system", "content": system}
        else:
            messages.insert(0, {"role": "system", "content": system})
    else:
        messages = [{"role": "system", "content": system}]
        history = payload.get("history") if isinstance(payload.get("history"), list) else []
        for row in history[-20:]:
            if not isinstance(row, dict):
                continue
            role = str(row.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            messages.append({"role": role, "content": str(row.get("content") or "")})
    user_content = message
    if payload.get("inject_retrieval", True):
        retrieved = retrieval.search(message, limit=3, sources=["knowledge"])
        snippets = []
        for hit in retrieved.get("hits") or []:
            snippets.append(f"- {hit.get('title') or hit.get('id')}: {hit.get('snippet')}")
        if snippets:
            user_content += "\n\n[Ivyea 本地知识检索]\n" + "\n".join(snippets)
    messages.append({"role": "user", "content": user_content})
    return messages, created_at


def _public_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        content = msg.get("content")
        if content is None:
            content = ""
        rows.append({"role": str(role), "content": security.redact_text(str(content))})
    return rows[-30:]


def _public_session(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id", ""),
        "updated": row.get("updated"),
        "turns": row.get("turns", 0),
        "preview": security.redact_text(str(row.get("preview") or "")),
    }


def _public_session_detail(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": data.get("id", ""),
        "created": data.get("created"),
        "updated": data.get("updated"),
        "model": data.get("model", ""),
        "usage": data.get("usage") or {},
        "messages": _public_messages(data.get("messages") or []),
    }


def _public_trace(row: dict[str, Any]) -> dict[str, Any]:
    payload = {}
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id", ""),
        "turn_id": row.get("turn_id", ""),
        "event": row.get("event", ""),
        "name": row.get("name", ""),
        "ok": bool(row.get("ok")),
        "duration_ms": int(row.get("duration_ms") or 0),
        "summary": security.redact_text(str(row.get("summary") or "")),
        "payload": security.redact_obj(payload),
        "ts": row.get("ts"),
    }


def _public_skill(sk: skills.Skill, include_body: bool = False) -> dict[str, Any]:
    row = {
        "id": sk.id,
        "title": sk.title,
        "domain": sk.domain,
        "version": sk.version,
        "description": sk.description,
        "triggers": list(sk.triggers),
        "knowledge_ids": list(sk.knowledge_ids),
        "tools": list(sk.tools),
        "scope": sk.scope,
    }
    if include_body:
        row["body"] = security.redact_text(sk.body)
        row["linked_knowledge"] = [
            _public_knowledge_card(card)
            for card in (knowledge.get_card(kid) for kid in sk.knowledge_ids)
            if card
        ]
    return row


def _public_knowledge_card(card: dict[str, Any], include_body: bool = False) -> dict[str, Any]:
    keys = [
        "id", "title", "category", "source_type", "confidence", "freshness",
        "source_quality", "retrieved_at", "license", "source_url", "tags",
        "scope", "body_hash", "score", "snippet",
    ]
    row = {key: card.get(key) for key in keys if key in card}
    if include_body:
        row["body"] = security.redact_text(str(card.get("body") or ""))
    return row


def _public_knowledge_audit(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": card.get("id", ""),
        "title": card.get("title", ""),
        "category": card.get("category", ""),
        "scope": card.get("scope", ""),
        "source_type": card.get("source_type", ""),
        "confidence": card.get("confidence", ""),
        "freshness": card.get("freshness", ""),
        "source_quality": card.get("source_quality", ""),
        "retrieved_at": card.get("retrieved_at", ""),
        "license": card.get("license", ""),
        "source_url": security.redact_text(str(card.get("source_url") or "")),
        "tags": list(card.get("tags") or []),
        "body_hash": card.get("body_hash", ""),
    }


def _public_install_info(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": info.get("version", ""),
        "python": info.get("python", ""),
        "prefix": info.get("prefix", ""),
        "method": info.get("method", ""),
        "ivyea_dir": info.get("ivyea_dir", ""),
        "ivyea_bin": info.get("ivyea_bin", ""),
        "pipx": info.get("pipx", ""),
        "platform": info.get("platform", ""),
    }


def _public_workspace_index(index: dict[str, Any], path: Any) -> dict[str, Any]:
    files = index.get("files") or []
    languages: dict[str, int] = {}
    for entry in files:
        lang = str(entry.get("language") or "Text")
        languages[lang] = languages.get(lang, 0) + 1
    return {
        "version": index.get("version"),
        "root": index.get("root", ""),
        "generated_at": index.get("generated_at", ""),
        "index_path": str(path),
        "file_count": len(files),
        "languages": dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
        "skipped": index.get("skipped") or {},
        "sample_files": [
            {
                "path": entry.get("path", ""),
                "language": entry.get("language", ""),
                "lines": entry.get("lines", 0),
                "symbols": list(entry.get("symbols") or [])[:8],
            }
            for entry in files[:30]
        ],
    }
