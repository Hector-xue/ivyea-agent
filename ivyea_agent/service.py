"""Local HTTP API for embedding IvyeaAgent in IvyeaOps."""
from __future__ import annotations

import json
import hmac
import hashlib
import os
import base64
import binascii
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import (
    __version__, ads_evidence, agent_loop, code_agent, config, knowledge, knowledge_evidence,
    knowledge_governance, knowledge_quality, knowledge_sync, models,
    progress_reporting, retrieval, security, self_manage, sessions, skills, task_runner,
    traces, workspace,
)
from .agent_tools import ToolContext


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def health() -> dict[str, Any]:
    model_cfg = config.get_model_config()
    provider = (
        models.provider_by_id(str(model_cfg.get("provider_id") or ""))
        or models.provider_by_id(str(model_cfg.get("provider") or ""))
        or model_cfg
    )
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
            "key_status": models.key_status(provider),
            "capabilities": models.provider_capabilities(provider),
            "badges": models.capability_badges(provider),
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
            "official_source_monitoring": True,
            "answer_citations": True,
            "authorized_account_evidence": True,
            "amazon_ads_evidence_analysis": True,
            "knowledge_governance_dashboard": True,
            "knowledge_change_review_ledger": True,
            "knowledge_quality_benchmark": True,
            "knowledge_version_history": True,
            "knowledge_rollback": True,
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
            {"method": "GET", "path": "/v1/model/providers", "description": "provider capability matrix without secrets"},
            {"method": "GET", "path": "/v1/model/providers/{id}/models", "description": "live/cache/builtin model catalog for one provider"},
            {"method": "POST", "path": "/v1/model/providers/{id}/probe", "description": "minimal provider connectivity probe without returning secrets"},
            {"method": "POST", "path": "/v1/model/configure", "description": "configure the active IvyeaAgent model without returning secrets"},
            {"method": "GET", "path": "/v1/mcp/self-config", "description": "stdio MCP server config for local clients"},
            {"method": "GET", "path": "/v1/system/status", "description": "install/runtime status for IvyeaOps diagnostics"},
            {"method": "GET", "path": "/v1/system/doctor", "description": "install/runtime doctor checks"},
            {"method": "GET", "path": "/v1/system/bootstrap", "description": "IvyeaOps local bootstrap and autodiscovery contract"},
            {"method": "GET", "path": "/v1/system/service/status", "description": "local service process/pid/health status"},
            {"method": "GET", "path": "/v1/system/service/logs", "description": "tail local service logs"},
            {"method": "POST", "path": "/v1/system/service/start", "description": "start the local IvyeaAgent service"},
            {"method": "POST", "path": "/v1/system/service/stop", "description": "stop the local IvyeaAgent service"},
            {"method": "POST", "path": "/v1/system/service/autostart", "description": "write local autostart template"},
            {"method": "GET", "path": "/v1/chat/sessions", "description": "list persisted embedded chat sessions"},
            {"method": "POST", "path": "/v1/chat/sessions", "description": "create an embedded chat session"},
            {"method": "POST", "path": "/v1/chat/sessions/import", "description": "seed a chat session with pre-existing messages (migration, no LLM turn)"},
            {"method": "GET", "path": "/v1/chat/sessions/{id}", "description": "load embedded chat session"},
            {"method": "POST", "path": "/v1/chat", "description": "run one read-only embedded agent turn"},
            {"method": "POST", "path": "/v1/chat/stream", "description": "run one read-only embedded agent turn as server-sent events"},
            {"method": "GET", "path": "/v1/skills", "description": "list active built-in and user skills"},
            {"method": "GET", "path": "/v1/skills/search", "description": "search active skills"},
            {"method": "GET", "path": "/v1/skills/{id}", "description": "load skill detail"},
            {"method": "GET", "path": "/v1/knowledge/cards", "description": "list bundled and user knowledge cards"},
            {"method": "POST", "path": "/v1/knowledge/cards", "description": "create a user-supplied knowledge card"},
            {"method": "GET", "path": "/v1/knowledge/cards/{id}", "description": "load knowledge card detail"},
            {"method": "GET", "path": "/v1/knowledge/files", "description": "list user knowledge files and uploaded source documents"},
            {"method": "GET", "path": "/v1/knowledge/file", "description": "read one user knowledge/upload file by relative path"},
            {"method": "DELETE", "path": "/v1/knowledge/file", "description": "delete one user knowledge/upload file by relative path"},
            {"method": "GET", "path": "/v1/knowledge/uploads", "description": "list knowledge upload history"},
            {"method": "POST", "path": "/v1/knowledge/upload", "description": "save an uploaded document, extract text, and build an import draft"},
            {"method": "POST", "path": "/v1/knowledge/uploads/apply", "description": "apply a confirmed upload draft into the knowledge base"},
            {"method": "GET", "path": "/v1/knowledge/audit", "description": "structured source quality and freshness audit"},
            {"method": "GET", "path": "/v1/knowledge/sources", "description": "knowledge source registry and review summary"},
            {"method": "GET", "path": "/v1/knowledge/watchlist", "description": "curated Amazon knowledge sources to review before import"},
            {"method": "GET", "path": "/v1/knowledge/official-sources", "description": "allowlisted official Amazon sources and monitoring policy"},
            {"method": "GET", "path": "/v1/knowledge/changes", "description": "official-source changes with review status"},
            {"method": "POST", "path": "/v1/knowledge/changes/review", "description": "record a confirmed review decision without publishing knowledge"},
            {"method": "GET", "path": "/v1/knowledge/changes/{event_id}/packet", "description": "load an approved source snapshot, diff, and candidate knowledge cards"},
            {"method": "POST", "path": "/v1/knowledge/changes/draft", "description": "prepare an evidence-linked runtime knowledge update draft"},
            {"method": "POST", "path": "/v1/knowledge/changes/apply", "description": "separately confirm and publish an approved evidence-linked runtime update"},
            {"method": "GET", "path": "/v1/knowledge/reviews", "description": "immutable official-source review history"},
            {"method": "GET", "path": "/v1/knowledge/publications", "description": "confirmed knowledge publications linked to reviewed source changes"},
            {"method": "GET", "path": "/v1/knowledge/versions", "description": "immutable user knowledge version history"},
            {"method": "POST", "path": "/v1/knowledge/versions/rollback", "description": "restore a confirmed user knowledge version"},
            {"method": "GET", "path": "/v1/knowledge/governance", "description": "knowledge review, freshness, coverage, and conflict dashboard"},
            {"method": "GET", "path": "/v1/knowledge/coverage", "description": "critical knowledge domain and marketplace coverage matrix"},
            {"method": "GET", "path": "/v1/knowledge/freshness", "description": "card freshness and official-source monitor status"},
            {"method": "GET", "path": "/v1/knowledge/quality", "description": "run deterministic Amazon knowledge retrieval quality cases"},
            {"method": "POST", "path": "/v1/knowledge/sync", "description": "check due public official sources without auto-publishing changes"},
            {"method": "GET", "path": "/v1/knowledge/evidence", "description": "list sanitized authorized account evidence metadata"},
            {"method": "GET", "path": "/v1/knowledge/evidence/schema", "description": "JSON Schema for authorized account evidence"},
            {"method": "GET", "path": "/v1/knowledge/ads/capabilities", "description": "dated Amazon Ads product, report, and evidence capability matrix"},
            {"method": "POST", "path": "/v1/knowledge/ads/analyze", "description": "analyze an Ads report or traffic experiment without persisting raw account data"},
            {"method": "POST", "path": "/v1/knowledge/evidence/draft", "description": "redact and structure authorized Seller Central evidence without storing it"},
            {"method": "POST", "path": "/v1/knowledge/evidence/apply", "description": "apply confirmed sanitized account evidence and rebuild indexes"},
            {"method": "GET", "path": "/v1/knowledge/conflicts", "description": "knowledge conflict review queue"},
            {"method": "POST", "path": "/v1/knowledge/update/draft", "description": "build a reviewed knowledge update draft with diff"},
            {"method": "POST", "path": "/v1/knowledge/update/apply", "description": "apply a confirmed knowledge update draft and rebuild indexes"},
            {"method": "POST", "path": "/v1/knowledge/import-directory", "description": "scan or import a legacy local knowledge directory into user knowledge"},
            {"method": "POST", "path": "/v1/knowledge/rebuild", "description": "validate knowledge metadata and rebuild local indexes"},
            {"method": "GET", "path": "/v1/knowledge/search", "description": "query bundled and user knowledge"},
            {"method": "GET", "path": "/v1/retrieval/embeddings", "description": "local embedding backend status"},
            {"method": "GET", "path": "/v1/retrieval/status", "description": "persistent local retrieval index status"},
            {"method": "POST", "path": "/v1/retrieval/search", "description": "unified local retrieval over knowledge and memory"},
            {"method": "POST", "path": "/v1/retrieval/embeddings", "description": "configure local embedding backend"},
            {"method": "POST", "path": "/v1/retrieval/embeddings/probe", "description": "probe configured local embedding backend"},
            {"method": "POST", "path": "/v1/retrieval/index", "description": "rebuild or sync persistent local retrieval index"},
            {"method": "GET", "path": "/v1/tasks", "description": "list tasks"},
            {"method": "POST", "path": "/v1/tasks", "description": "create task"},
            {"method": "GET", "path": "/v1/tasks/{id}", "description": "load task detail"},
            {"method": "GET", "path": "/v1/tasks/{id}/resume", "description": "load structured task resume prompt"},
            {"method": "POST", "path": "/v1/tasks/{id}/continue", "description": "continue a task from its structured resume prompt"},
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
            {"method": "POST", "path": "/v1/code/bundle", "description": "build a read-only multi-round code task bundle"},
            {"method": "POST", "path": "/v1/code/apply-loop", "description": "validate/apply/test one structured patch with repair audit"},
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


def model_providers() -> dict[str, Any]:
    return {"ok": True, "providers": models.provider_matrix()}


def _provider_secret(provider: dict[str, Any], payload_key: str = "") -> str:
    if payload_key:
        return payload_key
    config.load_env()
    auth = str(provider.get("auth_type") or "api_key").lower()
    if auth in ("oauth_external", "oauth_device_code", "copilot"):
        try:
            from . import oauth_auth
            return oauth_auth.resolve_provider_token(str(provider.get("id") or ""), str(provider.get("key_env") or ""), refresh=True)
        except Exception:
            return ""
    key_env = str(provider.get("key_env") or "")
    return os.environ.get(key_env, "") if key_env else ""


def model_provider_catalog(provider_id: str, refresh: bool = False) -> dict[str, Any]:
    provider = models.provider_by_id(provider_id)
    if not provider:
        return {"ok": False, "error": "provider_not_found", "provider_id": provider_id}
    return {
        "ok": True,
        "catalog": models.provider_model_catalog(
            provider,
            api_key=_provider_secret(provider),
            refresh=refresh,
        ),
    }


def model_provider_probe(provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    provider = models.provider_by_id(provider_id)
    if not provider:
        return {"ok": False, "error": "provider_not_found", "provider_id": provider_id}
    result = models.probe_provider(
        provider,
        api_key=_provider_secret(provider, str(payload.get("api_key") or "")),
        model=str(payload.get("model") or ""),
        timeout=float(payload.get("timeout") or 30.0),
    )
    return {"ok": bool(result.get("ok")), "probe": result}


_OPS_PROVIDER_ALIASES = {
    "google": "gemini",
    "gemini": "gemini",
    "kimi": "kimi-coding",
    "moonshot": "kimi",
}

_OPENAI_COMPAT_BASES = {
    "xiaomi": "https://token-plan-sgp.xiaomimimo.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "custom": "",
}

_OPENAI_COMPAT_KEY_ENVS = {
    "xiaomi": "XIAOMI_API_KEY",
    "groq": "GROQ_API_KEY",
    "together": "TOGETHER_API_KEY",
    "custom": "CUSTOM_API_KEY",
}


def _model_entry_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_provider = str(payload.get("provider_id") or payload.get("provider") or "").strip().lower()
    provider_id = _OPS_PROVIDER_ALIASES.get(raw_provider, raw_provider)
    entry = models.provider_by_id(provider_id)
    if entry:
        out = dict(entry)
    else:
        base = str(payload.get("base_url") or _OPENAI_COMPAT_BASES.get(raw_provider, "")).strip()
        key_env = str(payload.get("key_env") or _OPENAI_COMPAT_KEY_ENVS.get(raw_provider, "IVYEA_AGENT_MODEL_API_KEY")).strip()
        label = str(payload.get("label") or raw_provider or "Custom OpenAI-compatible").strip()
        out = {
            "id": raw_provider or "custom",
            "provider_id": raw_provider or "custom",
            "label": label,
            "kind": "openai",
            "api_mode": "chat_completions",
            "auth_type": "api_key" if key_env else "none",
            "base": base,
            "key_env": key_env,
            "models": [str(payload.get("model") or "").strip()] if payload.get("model") else [],
            "default_model": str(payload.get("model") or "").strip(),
            "status": "usable",
        }
    if payload.get("base_url"):
        out["base"] = str(payload.get("base_url") or "").strip()
    if payload.get("key_env"):
        out["key_env"] = str(payload.get("key_env") or "").strip()
    return out


def model_configure(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist active model settings for embedded IvyeaOps configuration.

    Secrets are written to ``~/.ivyea/.env`` and never returned. Unknown
    providers are treated as OpenAI-compatible when a base_url/model is supplied.
    """
    entry = _model_entry_from_payload(payload)
    model = str(payload.get("model") or entry.get("default_model") or entry.get("model") or "").strip()
    base_url = str(payload.get("base_url") or entry.get("base") or "").strip()
    if not model:
        return {"ok": False, "error": "model_required"}
    if (entry.get("kind") == "openai" or entry.get("api_mode") == "chat_completions") and not base_url:
        return {"ok": False, "error": "base_url_required"}

    config.apply_model(entry, model=model, base_url=base_url)
    api_key = payload.get("api_key")
    key_env = str(entry.get("key_env") or "").strip()
    if isinstance(api_key, str) and api_key:
        if key_env:
            config.set_env_key(key_env, api_key)
    elif payload.get("clear_api_key") and key_env:
        config.set_env_key(key_env, "")
    return {
        "ok": True,
        "model": health()["model"],
        "configured": {
            "provider": entry.get("id", ""),
            "provider_id": entry.get("provider_id", entry.get("id", "")),
            "model": model,
            "base_url": base_url,
            "key_env": key_env,
            "key_configured": bool(isinstance(api_key, str) and api_key) or bool(config.get_active_key()),
        },
    }


def task_detail(task_id: str) -> dict[str, Any]:
    return {"ok": True, "task": task_runner.load(task_id)}


def task_resume(task_id: str) -> dict[str, Any]:
    return task_runner.resume_payload(task_id)


def task_continue(task_id: str, payload: dict[str, Any], provider: Any | None = None) -> dict[str, Any]:
    task = task_runner.load(task_id)
    resume_before = task_runner.resume_payload(task_id)["resume"]
    model_cfg = config.get_model_config()
    api_key = config.get_active_key()
    if _model_requires_key(model_cfg) and not api_key and provider is None:
        return {
            "ok": False,
            "error": "model_not_configured",
            "model": health()["model"],
            "task": task,
            "resume": resume_before,
        }
    step = task_runner.next_step(task)
    if step and step.get("status") in {"pending", "blocked"}:
        task = task_runner.update_step(task_id, int(step["index"]), "in_progress", "continue requested")
    resume = task_runner.resume_payload(task_id)["resume"]
    extra = str(payload.get("message") or payload.get("instruction") or "").strip()
    message = str(resume.get("prompt") or task_runner.render_resume(task)).strip()
    if extra:
        message += "\n\n[本轮补充要求]\n" + extra
    state = resume.get("state") if isinstance(resume.get("state"), dict) else {}
    chat_payload = {
        **payload,
        "message": message,
        "task_id": task_id,
        "workspace": str(payload.get("workspace") or task.get("workspace") or ""),
        "session_id": str(payload.get("session_id") or state.get("session_id") or ""),
        "turn_id": str(payload.get("turn_id") or "task-continue"),
        "plan_mode": payload.get("plan_mode", True),
        "inject_retrieval": payload.get("inject_retrieval", False),
        "persist": payload.get("persist", True),
    }
    result = chat_run(chat_payload, provider=provider)
    return {
        "ok": bool(result.get("ok")),
        "task": task_runner.load(task_id),
        "resume": task_runner.resume_payload(task_id)["resume"],
        "chat": result,
    }


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


def system_bootstrap() -> dict[str, Any]:
    data = self_manage.ops_bootstrap(host=DEFAULT_HOST, port=DEFAULT_PORT)
    info = data.get("info") or {}
    data["info"] = _public_install_info(info)
    return data


def system_service_status(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload or {}
    return {
        "ok": True,
        "service": self_manage.service_status(
            host=str(body.get("host") or DEFAULT_HOST),
            port=_int(body.get("port"), DEFAULT_PORT),
            probe=body.get("probe") if isinstance(body.get("probe"), bool) else True,
        ),
    }


def system_service_logs(lines: int = 80) -> dict[str, Any]:
    return {"ok": True, "logs": self_manage.service_log_tail(lines=lines)}


def system_service_start(payload: dict[str, Any]) -> dict[str, Any]:
    result = self_manage.service_start(
        host=str(payload.get("host") or DEFAULT_HOST),
        port=_int(payload.get("port"), DEFAULT_PORT),
        allow_remote=bool(payload.get("allow_remote")),
        api_token=str(payload.get("api_token") or ""),
        wait=payload.get("wait") if isinstance(payload.get("wait"), bool) else True,
        timeout=float(payload.get("timeout") or 10),
    )
    return {"ok": bool(result.get("ok")), "result": result}


def system_service_stop(payload: dict[str, Any]) -> dict[str, Any]:
    result = self_manage.service_stop(
        timeout=float(payload.get("timeout") or 10),
        force=bool(payload.get("force")),
    )
    return {"ok": bool(result.get("ok")), "result": result}


def system_service_autostart(payload: dict[str, Any]) -> dict[str, Any]:
    result = self_manage.write_autostart(
        host=str(payload.get("host") or DEFAULT_HOST),
        port=_int(payload.get("port"), DEFAULT_PORT),
    )
    return {"ok": bool(result.get("ok")), "autostart": result}


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


def knowledge_sources() -> dict[str, Any]:
    data = knowledge.source_registry()
    return {"ok": True, "summary": data.get("summary") or {}, "sources": data.get("sources") or []}


def knowledge_watchlist() -> dict[str, Any]:
    data = knowledge.source_watchlist()
    return {"ok": True, "summary": data.get("summary") or {}, "sources": data.get("sources") or []}


def knowledge_official_sources() -> dict[str, Any]:
    data = knowledge_sync.registry()
    return {"ok": True, "summary": data["summary"], "sources": data["sources"]}


def knowledge_changes(limit: int = 50, review_status: str = "") -> dict[str, Any]:
    data = knowledge_sync.changes(limit=limit, review_status=review_status)
    return {"ok": True, **data}


def knowledge_reviews(limit: int = 100, event_id: str = "") -> dict[str, Any]:
    return {"ok": True, **knowledge_sync.review_history(limit=limit, event_id=event_id)}


def knowledge_publications(limit: int = 100, event_id: str = "") -> dict[str, Any]:
    return {"ok": True, **knowledge_sync.publication_history(limit=limit, event_id=event_id)}


def knowledge_change_review(payload: dict[str, Any]) -> dict[str, Any]:
    return knowledge_sync.review_change(
        str(payload.get("event_id") or ""),
        str(payload.get("decision") or ""),
        reviewer=str(payload.get("reviewer") or "local-operator"),
        reviewer_source=str(payload.get("reviewer_source") or "agent_api_token"),
        identity_verified=payload.get("identity_verified") is True,
        note=str(payload.get("note") or ""),
        confirm=payload.get("confirm") is True,
    )


def knowledge_versions(card_id: str = "", limit: int = 100) -> dict[str, Any]:
    return {"ok": True, **knowledge.list_versions(card_id, limit=limit)}


def knowledge_version_rollback(payload: dict[str, Any]) -> dict[str, Any]:
    return knowledge.rollback_version(
        str(payload.get("card_id") or ""),
        str(payload.get("version_id") or ""),
        confirm=payload.get("confirm") is True,
        rebuild_indexes=payload.get("rebuild") if isinstance(payload.get("rebuild"), bool) else True,
        actor=str(payload.get("actor") or "api-operator"),
        actor_source=str(payload.get("actor_source") or "agent_api_token"),
    )


def knowledge_change_packet(event_id: str, card_id: str = "") -> dict[str, Any]:
    return {"ok": True, "packet": knowledge_sync.change_packet(event_id, card_id=card_id)}


def knowledge_change_draft(payload: dict[str, Any]) -> dict[str, Any]:
    prepared = knowledge_sync.prepare_change_draft(
        str(payload.get("event_id") or ""),
        card_id=str(payload.get("card_id") or ""),
        body=str(payload.get("body") or ""),
        title=str(payload.get("title") or ""),
        new_card_id=str(payload.get("new_card_id") or ""),
    )
    public = dict(prepared)
    if isinstance(public.get("draft"), dict):
        public["draft"] = _public_knowledge_draft(public["draft"])
    return public


def knowledge_change_apply(payload: dict[str, Any]) -> dict[str, Any]:
    applied = knowledge_sync.apply_change_draft(
        str(payload.get("event_id") or ""),
        card_id=str(payload.get("card_id") or ""),
        body=str(payload.get("body") or ""),
        title=str(payload.get("title") or ""),
        new_card_id=str(payload.get("new_card_id") or ""),
        confirm=payload.get("confirm") is True,
        rebuild_indexes=payload.get("rebuild") if isinstance(payload.get("rebuild"), bool) else True,
    )
    public = dict(applied)
    if isinstance(public.get("draft"), dict):
        public["draft"] = _public_knowledge_draft(public["draft"])
    return public


def knowledge_governance_dashboard() -> dict[str, Any]:
    return knowledge_governance.dashboard()


def knowledge_coverage() -> dict[str, Any]:
    return {"ok": True, "coverage": knowledge_governance.coverage()}


def knowledge_freshness() -> dict[str, Any]:
    return {"ok": True, "freshness": knowledge_governance.freshness()}


def knowledge_quality_run() -> dict[str, Any]:
    result = knowledge_quality.run()
    return {"ok": bool(result.get("ok")), "quality": result}


def knowledge_sync_run(payload: dict[str, Any]) -> dict[str, Any]:
    source_ids = payload.get("source_ids") or []
    if isinstance(source_ids, str):
        source_ids = [part.strip() for part in source_ids.split(",") if part.strip()]
    if not isinstance(source_ids, list):
        raise ValueError("source_ids must be a list or comma-separated string")
    return knowledge_sync.sync(
        force=bool(payload.get("force", False)),
        source_ids=[str(value) for value in source_ids],
    )


def knowledge_evidence_list(limit: int = 100) -> dict[str, Any]:
    data = knowledge_evidence.list_evidence(limit=limit)
    return {"ok": True, **data}


def knowledge_evidence_draft(payload: dict[str, Any]) -> dict[str, Any]:
    prepared = knowledge_evidence.prepare(payload)
    prepared["draft"]["actor"] = str(payload.get("actor") or "api-operator")
    prepared["draft"]["actor_source"] = str(payload.get("actor_source") or "agent_api_token")
    return prepared


def knowledge_evidence_apply(payload: dict[str, Any]) -> dict[str, Any]:
    prepared = knowledge_evidence.prepare(payload)
    prepared["draft"]["actor"] = str(payload.get("actor") or "api-operator")
    prepared["draft"]["actor_source"] = str(payload.get("actor_source") or "agent_api_token")
    return knowledge_evidence.apply(
        prepared,
        confirm=bool(payload.get("confirm", False)),
        rebuild_indexes=payload.get("rebuild") if isinstance(payload.get("rebuild"), bool) else True,
    )


def knowledge_ads_capabilities() -> dict[str, Any]:
    return {"ok": True, "capabilities": ads_evidence.capability_matrix()}


def knowledge_ads_analyze(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "analysis": ads_evidence.analyze(payload), "raw_preserved": False}


def knowledge_update_draft(payload: dict[str, Any]) -> dict[str, Any]:
    body = str(payload.get("body") or payload.get("content") or "").strip()
    if not body:
        raise ValueError("body is required")
    draft = knowledge.draft_update(
        str(payload.get("title") or payload.get("id") or "用户知识"),
        body,
        source_url=str(payload.get("source_url") or ""),
        source_type=str(payload.get("source_type") or "user"),
        confidence=str(payload.get("confidence") or ""),
        tags=payload.get("tags"),
        card_id=str(payload.get("id") or payload.get("card_id") or ""),
        license=str(payload.get("license") or "user_supplied"),
    )
    return {"ok": True, "draft": _public_knowledge_draft(draft)}


def knowledge_update_apply(payload: dict[str, Any]) -> dict[str, Any]:
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
    if not draft or not draft.get("body"):
        body = str(payload.get("body") or payload.get("content") or "").strip()
        if not body:
            raise ValueError("body is required")
        draft = knowledge.draft_update(
            str(payload.get("title") or payload.get("id") or "用户知识"),
            body,
            source_url=str(payload.get("source_url") or ""),
            source_type=str(payload.get("source_type") or "user"),
            confidence=str(payload.get("confidence") or ""),
            tags=payload.get("tags"),
            card_id=str(payload.get("id") or payload.get("card_id") or ""),
            license=str(payload.get("license") or "user_supplied"),
        )
    result = knowledge.apply_update(
        draft,
        confirm=bool(payload.get("confirm")),
        rebuild_indexes=payload.get("rebuild") if isinstance(payload.get("rebuild"), bool) else True,
    )
    public = dict(result)
    if isinstance(public.get("draft"), dict):
        public["draft"] = _public_knowledge_draft(public["draft"])
    if isinstance(public.get("card"), dict):
        public["card"] = _public_knowledge_card(public["card"])
    return {"ok": bool(result.get("ok")), "result": public}


def knowledge_files(limit: int = 500) -> dict[str, Any]:
    data = knowledge.list_files(limit=limit)
    return {
        "ok": True,
        "root": data.get("root", ""),
        "uploads_root": data.get("uploads_root", ""),
        "uploads": data.get("uploads") or [],
        "cards": data.get("cards") or [],
        "history": [_public_knowledge_upload(row) for row in data.get("history") or []],
    }


def knowledge_file_read(path: str) -> dict[str, Any]:
    data = knowledge.read_file(path)
    return {"ok": True, "file": data}


def knowledge_file_delete(path: str) -> dict[str, Any]:
    return knowledge.delete_file(path)


def knowledge_uploads(limit: int = 50) -> dict[str, Any]:
    data = knowledge.list_uploads(limit=limit)
    return {"ok": True, "root": data.get("root", ""), "uploads": [_public_knowledge_upload(row) for row in data.get("uploads") or []]}


def knowledge_upload(payload: dict[str, Any]) -> dict[str, Any]:
    filename = str(payload.get("filename") or payload.get("name") or "upload.txt")
    raw = str(payload.get("content_base64") or payload.get("data_base64") or "")
    if not raw:
        raise ValueError("content_base64 is required")
    try:
        data = base64.b64decode(raw.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("invalid content_base64") from exc
    result = knowledge.upload_document(
        filename,
        data,
        title=str(payload.get("title") or ""),
        source_url=str(payload.get("source_url") or ""),
        source_type=str(payload.get("source_type") or "user"),
        confidence=str(payload.get("confidence") or ""),
        tags=payload.get("tags"),
        card_id=str(payload.get("id") or payload.get("card_id") or ""),
        license=str(payload.get("license") or "user_supplied"),
        confirm=bool(payload.get("confirm")),
        rebuild_indexes=payload.get("rebuild") if isinstance(payload.get("rebuild"), bool) else True,
    )
    public = {
        "ok": True,
        "upload": _public_knowledge_upload(result.get("upload") or {}),
        "extraction": result.get("extraction") or {},
        "draft": _public_knowledge_draft(result.get("draft") or {}),
    }
    if isinstance(result.get("apply"), dict):
        applied = dict(result["apply"])
        if isinstance(applied.get("card"), dict):
            applied["card"] = _public_knowledge_card(applied["card"])
        if isinstance(applied.get("draft"), dict):
            applied["draft"] = _public_knowledge_draft(applied["draft"])
        public["apply"] = applied
    return public


def knowledge_upload_apply(payload: dict[str, Any]) -> dict[str, Any]:
    upload_id = str(payload.get("upload_id") or payload.get("id") or "").strip()
    if not upload_id:
        raise ValueError("upload_id is required")
    result = knowledge.apply_upload(
        upload_id,
        confirm=bool(payload.get("confirm")),
        rebuild_indexes=payload.get("rebuild") if isinstance(payload.get("rebuild"), bool) else True,
    )
    public = {
        "ok": bool(result.get("ok")),
        "upload": _public_knowledge_upload(result.get("upload") or {}),
        "draft": _public_knowledge_draft(result.get("draft") or {}),
        "result": dict(result.get("result") or {}),
    }
    if isinstance(public["result"].get("card"), dict):
        public["result"]["card"] = _public_knowledge_card(public["result"]["card"])
    if isinstance(public["result"].get("draft"), dict):
        public["result"]["draft"] = _public_knowledge_draft(public["result"]["draft"])
    return public


def knowledge_import_directory(payload: dict[str, Any]) -> dict[str, Any]:
    result = knowledge.import_directory(
        str(payload.get("root") or payload.get("path") or ""),
        namespace=str(payload.get("namespace") or "gbrain"),
        confirm=bool(payload.get("confirm")),
        max_files=_int(payload.get("max_files"), 1000),
        max_file_bytes=_int(payload.get("max_file_bytes"), 5 * 1024 * 1024),
        rebuild_indexes=payload.get("rebuild") if isinstance(payload.get("rebuild"), bool) else True,
    )
    if payload.get("confirm") and (payload.get("rebuild") if isinstance(payload.get("rebuild"), bool) else True):
        result.setdefault("indexes", {})["retrieval"] = retrieval.rebuild_index()
    public = dict(result)
    public["imported"] = [
        {k: v for k, v in row.items() if k != "card"} | (
            {"card": _public_knowledge_card(row["card"])} if isinstance(row.get("card"), dict) else {}
        )
        for row in result.get("imported") or []
    ]
    return {"ok": bool(result.get("ok")), "import": public}


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


def code_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "bundle": code_agent.task_bundle(
            str(payload.get("goal") or ""),
            root=_root(payload),
            test_output=str(payload.get("test_output") or payload.get("output") or payload.get("text") or ""),
            limit=_int(payload.get("limit"), 8),
        ),
    }


def code_apply_loop(payload: dict[str, Any]) -> dict[str, Any]:
    spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
    return {
        "ok": True,
        "run": code_agent.patch_apply_loop(
            spec,
            root=_root(payload),
            test_command=str(payload.get("test_command") or payload.get("command") or ""),
            execute=bool(payload.get("execute")),
            timeout=_int(payload.get("timeout"), 120),
            persist=payload.get("persist") if isinstance(payload.get("persist"), bool) else True,
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
    if isinstance(payload.get("ops_bridge"), dict):
        ctx.ops_bridge = dict(payload.get("ops_bridge") or {})
    if isinstance(payload.get("ops_context"), dict):
        ctx.ops_context = dict(payload.get("ops_context") or {})
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
        text = agent_loop.run_turn(provider, ctx, messages, max_steps=(_int(payload.get("max_steps"), 0) or None), narrate=narrate)
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
        "progress": progress_reporting.public_state(ctx),
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
    if isinstance(payload.get("ops_bridge"), dict):
        ctx.ops_bridge = dict(payload.get("ops_bridge") or {})
    if isinstance(payload.get("ops_context"), dict):
        ctx.ops_context = dict(payload.get("ops_context") or {})
    ctx.session_id = str(payload.get("session_id") or "") or sessions.new_id()
    ctx.turn_id = str(payload.get("turn_id") or "")
    if payload.get("asin"):
        ctx.asin = str(payload.get("asin") or "")

    try:
        messages, created_at = _chat_messages(message, payload, ctx)
    except ValueError as exc:
        data = {"ok": False, "error": str(exc)}
        send("error", data)
        return data
    send("start", {"ok": True, "session_id": ctx.session_id, "read_only": bool(plan_mode), "model": health()["model"]})

    def narrate(text: str) -> None:
        send("event", {"type": "event", "text": security.redact_text(str(text))})

    try:
        provider = provider or build_chain(model_cfg, api_key, narrate=narrate)
        out = agent_loop.run_turn_stream(
            provider,
            ctx,
            messages,
            max_steps=(_int(payload.get("max_steps"), 0) or None),
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
        "progress": progress_reporting.public_state(ctx),
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


def chat_session_delete(session_id: str) -> dict[str, Any]:
    if not sessions.delete(session_id):
        raise FileNotFoundError(f"会话不存在：{session_id}")
    return {"ok": True, "deleted": session_id}


def chat_session_create(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("id") or "") or sessions.new_id()
    initial = str(payload.get("message") or payload.get("title") or "").strip()
    messages: list[dict[str, Any]] = []
    if initial:
        messages.append({"role": "user", "content": initial})
    sessions.save(session_id, messages, model=config.get_model_config().get("model", ""))
    data = sessions.load(session_id) or {"id": session_id, "messages": messages}
    return {"ok": True, "session": _public_session_detail(data)}


def chat_session_import(payload: dict[str, Any]) -> dict[str, Any]:
    """Seed a persisted session with pre-existing messages (no LLM turn).

    Used to migrate an external transcript store into the embedded session
    library so both callers share one history. Only plain text turns are kept."""
    raw = payload.get("messages")
    messages: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for m in raw:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "")
            content = m.get("content")
            if role in {"system", "user", "assistant"} and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
    if not messages:
        return {"ok": False, "error": "no messages"}
    session_id = str(payload.get("id") or "") or sessions.new_id()
    created = payload.get("created")
    sessions.save(
        session_id,
        messages,
        model=str(payload.get("model") or config.get_model_config().get("model", "")),
        created=float(created) if isinstance(created, (int, float)) else None,
    )
    return {"ok": True, "id": session_id, "turns": sum(1 for m in messages if m["role"] == "user")}


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
        if parsed.path == "/v1/model/providers":
            self._json(200, model_providers())
            return
        if parsed.path.startswith("/v1/model/providers/") and parsed.path.endswith("/models"):
            parts = parsed.path.strip("/").split("/")
            provider_id = parts[3] if len(parts) >= 5 else ""
            self._json(200, model_provider_catalog(provider_id, refresh=(_first(qs, "refresh") in ("1", "true", "yes"))))
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
        if parsed.path == "/v1/system/bootstrap":
            self._json(200, system_bootstrap())
            return
        if parsed.path == "/v1/system/service/status":
            self._json(200, system_service_status({
                "host": _first(qs, "host") or DEFAULT_HOST,
                "port": _int(_first(qs, "port"), DEFAULT_PORT),
            }))
            return
        if parsed.path == "/v1/system/service/logs":
            self._json(200, system_service_logs(lines=_int(_first(qs, "lines"), 80)))
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
        if parsed.path == "/v1/knowledge/files":
            self._json(200, knowledge_files(limit=_int(_first(qs, "limit"), 500)))
            return
        if parsed.path == "/v1/knowledge/file":
            try:
                self._json(200, knowledge_file_read(_first(qs, "path")))
            except (FileNotFoundError, ValueError) as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/uploads":
            self._json(200, knowledge_uploads(limit=_int(_first(qs, "limit"), 50)))
            return
        if parsed.path == "/v1/knowledge/audit":
            self._json(200, knowledge_audit())
            return
        if parsed.path == "/v1/knowledge/sources":
            self._json(200, knowledge_sources())
            return
        if parsed.path == "/v1/knowledge/watchlist":
            self._json(200, knowledge_watchlist())
            return
        if parsed.path == "/v1/knowledge/official-sources":
            self._json(200, knowledge_official_sources())
            return
        if parsed.path == "/v1/knowledge/changes":
            try:
                self._json(200, knowledge_changes(
                    limit=_int(_first(qs, "limit"), 50), review_status=_first(qs, "status"),
                ))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/reviews":
            self._json(200, knowledge_reviews(
                limit=_int(_first(qs, "limit"), 100), event_id=_first(qs, "event_id"),
            ))
            return
        if parsed.path == "/v1/knowledge/publications":
            self._json(200, knowledge_publications(
                limit=_int(_first(qs, "limit"), 100), event_id=_first(qs, "event_id"),
            ))
            return
        if parsed.path == "/v1/knowledge/versions":
            self._json(200, knowledge_versions(
                card_id=_first(qs, "card_id"), limit=_int(_first(qs, "limit"), 100),
            ))
            return
        if parsed.path.startswith("/v1/knowledge/changes/") and parsed.path.endswith("/packet"):
            parts = parsed.path.strip("/").split("/")
            event_id = parts[3] if len(parts) == 5 else ""
            try:
                self._json(200, knowledge_change_packet(event_id, card_id=_first(qs, "card_id")))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/governance":
            self._json(200, knowledge_governance_dashboard())
            return
        if parsed.path == "/v1/knowledge/coverage":
            self._json(200, knowledge_coverage())
            return
        if parsed.path == "/v1/knowledge/freshness":
            self._json(200, knowledge_freshness())
            return
        if parsed.path == "/v1/knowledge/quality":
            result = knowledge_quality_run()
            self._json(200, result)
            return
        if parsed.path == "/v1/knowledge/evidence":
            self._json(200, knowledge_evidence_list(limit=_int(_first(qs, "limit"), 100)))
            return
        if parsed.path == "/v1/knowledge/evidence/schema":
            self._json(200, {"ok": True, "schema": knowledge_evidence.schema()})
            return
        if parsed.path == "/v1/knowledge/ads/capabilities":
            self._json(200, knowledge_ads_capabilities())
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
            parts = parsed.path.strip("/").split("/")
            task_id = parts[2] if len(parts) >= 3 else ""
            try:
                if len(parts) >= 4 and parts[3] == "resume":
                    self._json(200, task_resume(task_id))
                else:
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
            # 心跳：单个慢工具（如市场调研 MCP）可能几分钟不产出任何 SSE 事件，
            # 中间代理/客户端的"单次 read 静默超时"会掐断仍在健康执行的轮次。
            # 每 15s 写一行 SSE 注释（": ping"）保持链路有字节流动；注释行没有
            # data 字段，所有标准 SSE 解析器都会忽略。写锁保证与事件写入互斥。
            write_lock = threading.Lock()
            done = threading.Event()
            client_gone = threading.Event()

            def _locked_send(event: str, data: dict[str, Any]) -> None:
                # 客户端断开不打断轮次：写失败后降级为"无声跑完"，让 chat_stream
                # 正常收尾并把完整会话落盘——用户随后能在历史会话里拿到回答。
                if client_gone.is_set():
                    return
                try:
                    with write_lock:
                        self._sse_send(event, data)
                except Exception:
                    client_gone.set()

            def _heartbeat() -> None:
                while not done.wait(15.0):
                    if client_gone.is_set():
                        return
                    try:
                        with write_lock:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    except Exception:
                        client_gone.set()
                        return  # 客户端已断开：心跳退出，轮次本身继续跑

            beat = threading.Thread(target=_heartbeat, daemon=True, name="chat-stream-heartbeat")
            beat.start()
            try:
                chat_stream(body, _locked_send)
            finally:
                done.set()
            return
        if parsed.path == "/v1/chat":
            try:
                self._json(200, chat_run(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/chat/sessions/import":
            self._json(200, chat_session_import(body))
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
        if parsed.path == "/v1/knowledge/update/draft":
            try:
                self._json(200, knowledge_update_draft(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/update/apply":
            try:
                self._json(200, knowledge_update_apply(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/upload":
            try:
                self._json(200, knowledge_upload(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/uploads/apply":
            try:
                self._json(200, knowledge_upload_apply(body))
            except FileNotFoundError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/import-directory":
            try:
                self._json(200, knowledge_import_directory(body))
            except FileNotFoundError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/rebuild":
            self._json(200, knowledge_rebuild())
            return
        if parsed.path == "/v1/knowledge/sync":
            try:
                self._json(200, knowledge_sync_run(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/changes/review":
            try:
                result = knowledge_change_review(self._verified_review_payload(body))
                self._json(200 if result.get("ok") else 409, result)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/versions/rollback":
            try:
                result = knowledge_version_rollback(body)
                self._json(200 if result.get("ok") else 409, result)
            except (ValueError, OSError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/changes/draft":
            try:
                self._json(200, knowledge_change_draft(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/changes/apply":
            try:
                result = knowledge_change_apply(body)
                self._json(200 if result.get("ok") else 409, result)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/evidence/draft":
            try:
                self._json(200, knowledge_evidence_draft(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/evidence/apply":
            try:
                result = knowledge_evidence_apply(body)
                self._json(200 if result.get("ok") else 409, result)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/knowledge/ads/analyze":
            try:
                self._json(200, knowledge_ads_analyze(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/v1/model/providers/") and parsed.path.endswith("/probe"):
            parts = parsed.path.strip("/").split("/")
            provider_id = parts[3] if len(parts) >= 5 else ""
            self._json(200, model_provider_probe(provider_id, body))
            return
        if parsed.path == "/v1/model/configure":
            self._json(200, model_configure(body))
            return
        if parsed.path == "/v1/system/service/start":
            self._json(200, system_service_start(body))
            return
        if parsed.path == "/v1/system/service/stop":
            self._json(200, system_service_stop(body))
            return
        if parsed.path == "/v1/system/service/autostart":
            self._json(200, system_service_autostart(body))
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
            self._json(200, retrieval.sync_index() if body.get("sync") else retrieval.rebuild_index())
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
                    if action == "continue":
                        self._json(200, task_continue(task_id, body))
                    else:
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
        if parsed.path == "/v1/code/bundle":
            self._json(200, code_bundle(body))
            return
        if parsed.path == "/v1/code/apply-loop":
            self._json(200, code_apply_loop(body))
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

    def do_DELETE(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/v1/knowledge/file":
            try:
                self._json(200, knowledge_file_delete(_first(qs, "path")))
            except (FileNotFoundError, ValueError) as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/v1/chat/sessions/"):
            session_id = parsed.path.rsplit("/", 1)[-1]
            try:
                self._json(200, chat_session_delete(session_id))
            except FileNotFoundError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
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

    def _verified_review_payload(self, body: dict[str, Any]) -> dict[str, Any]:
        """Verify an IvyeaOps admin identity assertion with the API token."""
        clean = dict(body)
        clean["identity_verified"] = False
        assertion = clean.pop("identity_assertion", None)
        token = str(getattr(self.server, "api_token", "") or "")
        if (
            not token
            or clean.get("reviewer_source") != "ops_authenticated_admin"
            or not isinstance(assertion, dict)
        ):
            return clean
        timestamp = str(assertion.get("timestamp") or "")
        signature = str(assertion.get("signature") or "")
        try:
            fresh = abs(time.time() - int(timestamp)) <= 300
        except ValueError:
            fresh = False
        material = "|".join([
            str(clean.get("event_id") or ""),
            str(clean.get("decision") or ""),
            str(clean.get("reviewer") or ""),
            timestamp,
        ])
        expected = hmac.new(token.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()
        clean["identity_verified"] = bool(fresh and signature and hmac.compare_digest(signature, expected))
        return clean

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
    system = agent_loop.SYSTEM_PROMPT + agent_loop.runtime_context_note()
    if ctx.plan_mode:
        system += agent_loop.PLAN_NOTE
    system += "\n\n[IvyeaOps 嵌入模式] 当前默认只读。需要写入广告、文件或执行命令时，先输出计划和审批项，不要在本轮直接执行。"
    if ctx.ops_bridge:
        current_board = str((ctx.ops_context or {}).get("board") or (ctx.ops_context or {}).get("pathname") or "").strip()
        system += (
            "\n\n[IvyeaOps 板块工具桥 — 最高优先级，必须遵守]\n"
            "你嵌在 IvyeaOps 工作台。当用户的请求属于下面这些板块任务时，你**唯一正确的做法是调用对应板块工具**。"
            "**严禁自己撰写报告正文、严禁仅凭知识库检索或常识拼凑答案**——只有板块工具才会用 IvyeaOps 接好的"
            "真实数据源（Sorftime / 卖家精灵）采集 + 合成，并把报告存进对应板块历史（用户要的就是这个结果）。"
            "你自己手写的报告不算数、不会进历史，等于没做。\n"
            "收到这类请求时，**第一步就直接调用工具，不要先长篇分析或解释**：\n"
            "- 市场调研 / 出市场调研报告 → `ivyea_ops_call_tool`，name=`market_generate_report`，"
            "arguments={\"query\": 关键词或ASIN, \"mode\": \"keyword\" 或 \"asin\", \"marketplace\": 站点如\"US\"}\n"
            "- 打法 / Launch 方案 → `playbook_generate_report`（同样 query/mode/marketplace）\n"
            "- 关键词竞争 / 竞品反查 / 流量诊断 → `deep_generate_report`\n"
            "- Listing 相关 → 对应 listing 工具\n"
            "不确定工具确切名字/参数时，先 `ivyea_ops_list_tools` 查再调。工具是长任务，调用后把结果与"
            "「已存入对应板块历史」告诉用户。只有当用户明确说「别用板块、你自己分析就行」时，才可以不调工具。"
        )
        if current_board:
            system += f"\n当前页面/板块：{current_board}"
        if ctx.ops_context:
            try:
                system += "\n当前页面上下文：" + json.dumps(ctx.ops_context, ensure_ascii=False, default=str)[:2000]
            except (TypeError, ValueError):
                pass
    if payload.get("system"):
        system += "\n\n[调用方系统上下文]\n" + str(payload.get("system") or "")
    # Explicit skill injection: caller passes `skill` (id) to load a built-in /
    # user skill's playbook into this turn's system prompt. Unlike retrieval
    # (which injects knowledge only), this makes the skill body actually present
    # so the agent follows it instead of trying to discover it on the filesystem.
    skill_id = str(payload.get("skill") or "").strip()
    if skill_id:
        sk = skills.get_skill(skill_id)
        if sk:
            system += "\n\n[必须遵循的技能 Skill]\n" + skills.render_skill(sk)
        else:
            system += f"\n\n[提示] 调用方请求的技能 `{skill_id}` 未找到，请按通用流程处理。"
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
        evidence = knowledge.evidence_context(message, limit=4)
        ctx.knowledge_citations = list(evidence.get("citations") or [])
        ctx.knowledge_retrieval_expected = bool(evidence.get("should_retrieve"))
        ctx.knowledge_risk = str(evidence.get("risk") or "none")
        ctx.knowledge_query = message
        if evidence.get("text"):
            user_content += (
                "\n\n[Ivyea 本地知识检索 / 亚马逊知识证据]\n" + str(evidence["text"])
                + "\n要求：采用摘录时在对应事实句末引用 [K#]；区分官方事实、账户观测、分析推断和运营假设。"
                + "广告指标必须保留报表、时间、币种、归因窗口/模型和销售范围；归因销售不等于增量销售，账户现象不等于官方算法。"
            )
    else:
        ctx.knowledge_citations = []
        ctx.knowledge_retrieval_expected = False
        ctx.knowledge_risk = "none"
        ctx.knowledge_query = message
    messages.append({"role": "user", "content": _with_payload_images(user_content, payload)})
    return messages, created_at


def _with_payload_images(user_content: str, payload: dict[str, Any]):
    """可选多模态：payload["images"] 为 data URI 列表时，把 user 消息升级成
    OpenAI 多模态 list-content（provider 适配器各自转换，codex→input_image、
    anthropic→image block）。主脑无视觉能力时显式报错——复核/看图类调用
    不允许静默丢图后瞎编结论。"""
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        return user_content
    uris = [str(u) for u in images if isinstance(u, str) and u.startswith("data:image/")][:4]
    if not uris:
        return user_content
    from . import config as _config
    from .vision import main_has_vision
    if not main_has_vision(_config.get_model_config()):
        raise ValueError("main_brain_no_vision: 当前主脑不支持图片输入，无法处理带图请求")
    parts: list[dict[str, Any]] = [{"type": "text", "text": user_content}]
    for uri in uris:
        parts.append({"type": "image_url", "image_url": {"url": uri}})
    return parts


def _public_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        content = msg.get("content")
        if content is None:
            content = ""
        if isinstance(content, list):  # 多模态：只回显文本部分，不吐 base64
            texts = [str(p.get("text") or "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            imgs = sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
            content = "\n".join(t for t in texts if t) + (f"\n[附图 {imgs} 张]" if imgs else "")
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
        "scope", "body_hash", "score", "snippet", "authority_tier", "evidence_class",
        "marketplaces", "locales", "evidence_id", "evidence_kind", "observed_at", "diagnostic",
    ]
    row = {key: card.get(key) for key in keys if key in card}
    if include_body:
        row["body"] = security.redact_text(str(card.get("body") or ""))
    return row


def _public_knowledge_draft(draft: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "ok", "action", "card_id", "title", "source_url", "source_type",
        "confidence", "license", "tags", "old_hash", "new_hash", "old_scope",
        "diff", "warnings", "review_required",
    ]
    row = {key: draft.get(key) for key in keys if key in draft}
    if "source_url" in row:
        row["source_url"] = security.redact_text(str(row.get("source_url") or ""))
    if "diff" in row:
        row["diff"] = security.redact_text(str(row.get("diff") or ""))
    return row


def _public_knowledge_upload(row: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id", "filename", "title", "raw_path", "extracted_path", "size",
        "created_at", "source_url", "source_type", "confidence", "license",
        "tags", "card_id", "warnings", "text_chars", "body_hash",
        "import_status", "imported_at",
    ]
    out = {key: row.get(key) for key in keys if key in row}
    if "source_url" in out:
        out["source_url"] = security.redact_text(str(out.get("source_url") or ""))
    return out


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
