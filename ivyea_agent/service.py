"""Local HTTP API for embedding IvyeaAgent in IvyeaOps."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, agent_loop, config, knowledge, models, retrieval, security, task_runner
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
            "remote_bind_requires": "--allow-remote",
            "secrets_in_responses": False,
        },
        "capabilities": {
            "health": True,
            "knowledge_search": True,
            "local_retrieval": retrieval.capabilities(),
            "task_state": True,
            "chat": True,
            "write_execution": False,
        },
        "endpoints": [
            {"method": "GET", "path": "/health", "description": "health, version, model status, knowledge and retrieval summary"},
            {"method": "GET", "path": "/v1/manifest", "description": "IvyeaOps integration manifest"},
            {"method": "GET", "path": "/v1/capabilities", "description": "retrieval capabilities"},
            {"method": "GET", "path": "/v1/model", "description": "current model status without secrets"},
            {"method": "POST", "path": "/v1/chat", "description": "run one read-only embedded agent turn"},
            {"method": "GET", "path": "/v1/knowledge/search", "description": "query bundled and user knowledge"},
            {"method": "GET", "path": "/v1/retrieval/embeddings", "description": "local embedding backend status"},
            {"method": "GET", "path": "/v1/retrieval/status", "description": "persistent local retrieval index status"},
            {"method": "POST", "path": "/v1/retrieval/search", "description": "unified local retrieval over knowledge and memory"},
            {"method": "POST", "path": "/v1/retrieval/embeddings", "description": "configure local embedding backend"},
            {"method": "POST", "path": "/v1/retrieval/index", "description": "rebuild persistent local retrieval index"},
            {"method": "GET", "path": "/v1/tasks", "description": "list tasks"},
            {"method": "POST", "path": "/v1/tasks", "description": "create task"},
            {"method": "GET", "path": "/v1/tasks/{id}", "description": "load task detail"},
            {"method": "POST", "path": "/v1/tasks/{id}/start", "description": "start next task step"},
            {"method": "POST", "path": "/v1/tasks/{id}/step", "description": "update a task step"},
            {"method": "POST", "path": "/v1/tasks/{id}/status", "description": "update task status"},
            {"method": "POST", "path": "/v1/tasks/{id}/log", "description": "append task log"},
        ],
    }


def task_list(limit: int = 20, status: str = "") -> dict[str, Any]:
    return {"ok": True, "tasks": task_runner.list_tasks(limit=limit, status=status or "")}


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
    ctx.session_id = str(payload.get("session_id") or "")
    ctx.turn_id = str(payload.get("turn_id") or "")
    if payload.get("asin"):
        ctx.asin = str(payload.get("asin") or "")

    messages = _chat_messages(message, payload, ctx)
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
    return result


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, int(port)), _Handler)


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = make_server(host, port)
    actual_host, actual_port = server.server_address
    print(f"Ivyea Agent API listening on http://{actual_host}:{actual_port}")
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
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path in ("/health", "/v1/health"):
            self._json(200, health())
            return
        if parsed.path == "/v1/manifest":
            self._json(200, manifest())
            return
        if parsed.path == "/v1/capabilities":
            self._json(200, {"ok": True, "retrieval": retrieval.capabilities()})
            return
        if parsed.path == "/v1/model":
            self._json(200, {"ok": True, "model": health()["model"]})
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
        if parsed.path.startswith("/v1/tasks/"):
            task_id = parsed.path.split("/", 3)[-1]
            try:
                self._json(200, task_detail(task_id))
            except FileNotFoundError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not_found", "path": parsed.path})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path == "/v1/chat":
            try:
                self._json(200, chat_run(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
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
            self._json(200, {
                "ok": True,
                "embeddings": retrieval.configure_embeddings(
                    backend=str(body.get("backend") or ""),
                    model=str(body.get("model") or ""),
                    model_path="" if model_path is None and "model_path" in body else (
                        str(model_path) if model_path is not None else None
                    ),
                    allow_download=body.get("allow_download") if isinstance(body.get("allow_download"), bool) else None,
                ),
            })
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

    def _json(self, status: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _first(qs: dict[str, list[str]], key: str) -> str:
    vals = qs.get(key) or []
    return vals[0] if vals else ""


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


def _chat_messages(message: str, payload: dict[str, Any], ctx: ToolContext) -> list[dict[str, Any]]:
    system = agent_loop.SYSTEM_PROMPT
    if ctx.plan_mode:
        system += agent_loop.PLAN_NOTE
    system += "\n\n[IvyeaOps 嵌入模式] 当前默认只读。需要写入广告、文件或执行命令时，先输出计划和审批项，不要在本轮直接执行。"
    if payload.get("system"):
        system += "\n\n[调用方系统上下文]\n" + str(payload.get("system") or "")
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
    return messages


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
