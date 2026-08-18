"""Local HTTP API for embedding IvyeaAgent in IvyeaOps."""
from __future__ import annotations

import json
import hmac
import hashlib
import os
import base64
import binascii
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import (
    __version__, ads_evidence, agent_loop, code_agent, config, context, knowledge, knowledge_evidence,
    knowledge_governance, knowledge_quality, knowledge_sync, models,
    progress_reporting, retrieval, routing, security, self_manage, sessions, skills, stream_json,
    task_runner, traces, transcript, workspace,
)
from .agent_tools import ToolContext


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# ---------------------------------------------------------------------------
# 远程人在环审批
#
# CLI 里写操作会弹 tui.select 等人选；嵌进网页后没有 TTY，所以 serve 一直强制
# 只读（execute=False + plan_mode=True），写工具直接回一句"计划模式：不执行"。
# 这条通道把那张审批卡投影到网页上：审批引擎、选项、语义完全复用 permission.py，
# 只是把"在终端等一个按键"换成"在队列上等一个 HTTP 决策"。
#
# 每个待决策请求一个单槽队列，键是 request_id。跑轮次的那个请求线程阻塞在
# queue.get 上；决策由 POST /v1/chat/permission 从另一个线程 put 进来
# （ThreadingHTTPServer 每请求一线程，不会互相饿死）。
#
# 三条兜底路径都收敛到"拒绝"，绝不把 agent 永久挂在一个没人会回的确认上：
# 超时、客户端断开、以及进程重启（队列在内存里，重启即失效，轮次本身也没了）。
# ---------------------------------------------------------------------------
_PENDING_APPROVALS: dict[str, "queue.Queue[str]"] = {}
_APPROVALS_LOCK = threading.Lock()
DEFAULT_APPROVAL_TIMEOUT = 600.0   # 10 分钟没人理 = 拒绝

# ── 历史会话详情 ────────────────────────────────────────────────────────────
# 一页几轮。按**轮**而不是按条：一次提问能产生几十条消息（本机实测 2 次提问 → 62 条），
# 按条切必然把用户自己发的那句话挤出窗口。
_DETAIL_TURNS_DEFAULT = 8
_DETAIL_TURNS_MAX = 100
# 详情里 tool 结果的单条上限。它从来没被界面渲染过（前端只留 user/assistant），
# 但本机最大那个会话里它占了 278KB —— 全量拖回去只是让每次打开会话更慢。
_DETAIL_TOOL_CONTENT_MAX = 1000


def resolve_permission(request_id: str, choice: str) -> bool:
    """回送一个审批决策，解开阻塞中的那一步。未知/已过期的 request_id 返回 False。"""
    with _APPROVALS_LOCK:
        slot = _PENDING_APPROVALS.get(str(request_id or ""))
    if slot is None:
        return False
    try:
        slot.put_nowait(str(choice or ""))
    except queue.Full:
        return False    # 已经有决策在路上，忽略重复提交
    return True


def pending_permissions() -> list[str]:
    with _APPROVALS_LOCK:
        return list(_PENDING_APPROVALS.keys())


class RemoteApproval:
    """permission.PromptFn 的远程实现：发事件 → 阻塞等决策 → 返回选项 key。"""

    def __init__(self, send: Any, session_id: str,
                 client_gone: "threading.Event | None" = None,
                 timeout: float = DEFAULT_APPROVAL_TIMEOUT) -> None:
        self._send = send
        self._session_id = session_id
        self._client_gone = client_gone
        self._timeout = float(timeout)

    def prompt(self, title: str, body: str, options: list, meta: dict) -> str:
        keys = [str(o[0]) for o in options]
        fallback = "deny" if "deny" in keys else (keys[-1] if keys else "deny")
        request_id = uuid.uuid4().hex[:16]
        slot: "queue.Queue[str]" = queue.Queue(maxsize=1)
        with _APPROVALS_LOCK:
            _PENDING_APPROVALS[request_id] = slot
        deadline = time.time() + self._timeout
        try:
            self._send("permission_request", {
                "request_id": request_id,
                "session_id": self._session_id,
                "op_type": str(meta.get("op_type") or ""),
                "title": title,
                "preview": body,
                "options": [{"key": str(k), "label": str(label)} for k, label in options],
                "destructive": bool(meta.get("destructive", True)),
                "expires_at": deadline,
            })
            # 分段等待而不是一次 get(timeout=600)：这样客户端一断开就能尽早收摊，
            # 不用把那一步在服务端干挂十分钟。
            while True:
                try:
                    choice = slot.get(timeout=1.0)
                except queue.Empty:
                    if self._client_gone is not None and self._client_gone.is_set():
                        return fallback     # 页面已经关了，没人能确认了 → 拒绝
                    if time.time() >= deadline:
                        self._send("permission_timeout", {"request_id": request_id})
                        return fallback
                    continue
                # 只认这次真发出去的选项，别让前端塞个奇怪的值改变语义。
                return choice if choice in keys else fallback
        finally:
            with _APPROVALS_LOCK:
                _PENDING_APPROVALS.pop(request_id, None)


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
        # 视觉三档链的实时状态。IvyeaOps 判"agent 能不能接带图任务"要看
        # vision_chain.effective，**不要**再看 model.capabilities.vision——
        # 后者只说明主脑本身，主脑没视觉不等于这条链没视觉（还有 T2/T3）。
        "vision_chain": _vision_chain_status(),
    }


def _vision_chain_status() -> dict[str, Any]:
    """/health 里的视觉链快照。绝不能因为它出错而让整个 /health 挂掉——
    ops 的自动启动、状态卡、模型同步全靠 /health 活着。"""
    try:
        from . import vision
        return vision.chain_status()
    except Exception as exc:  # noqa: BLE001
        return {"tier": 0, "effective": False, "error": str(exc)}


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
            {"method": "GET", "path": "/v1/config/vision", "description": "vision fallback chain status (tier 1 main brain / 2 sidecar / 3 local CV)"},
            {"method": "POST", "path": "/v1/config/vision", "description": "configure the tier-2 sidecar vision model without returning secrets"},
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


def vision_configure(payload: dict[str, Any]) -> dict[str, Any]:
    """配置 T2 的 sidecar 视觉模型（IvyeaOps 的"独立视觉槽"下推到这里）。

    存进 config 的 `vision_slot`，`vision.pick_vision_model()` 最优先读它。
    key 落 `~/.ivyea/.env`（复用 model_configure 同一套 set_env_key），不回显。

    为什么要下推而不是让 agent 自己配一遍：同一个视觉模型在 IvyeaOps 界面配一次
    就该同时对网页和 CLI 生效，两边各配一份必然长期不一致。

    空 model 视为**清除**视觉槽——这样"取消配置"有确定路径，否则用户只能去手改
    配置文件。
    """
    provider = str(payload.get("provider") or "").strip().lower()
    model = str(payload.get("model") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()

    if not model:
        config.set_setting("vision_slot", {})
        return {"ok": True, "cleared": True, "vision_chain": _vision_chain_status()}

    entry = models.provider_by_id(provider) if provider else None
    if entry is None and not base_url:
        return {"ok": False, "error": "unknown_provider_requires_base_url",
                "detail": f"provider={provider or '(空)'} 不在内置目录里，必须同时提供 base_url。"}

    slot = {
        "provider": provider or "custom",
        "model": model,
        "base_url": base_url or str((entry or {}).get("base") or ""),
    }
    # key 有两条落法：内置 provider 有 key_env 就写进 .env（与主脑同一套密钥管理），
    # 自定义端点没有 key_env，就只能连同槽位一起存 —— 后者由 config 文件权限保护。
    key_env = str((entry or {}).get("key_env") or "").strip()
    if api_key and key_env:
        config.set_env_key(key_env, api_key)
    elif api_key:
        slot["api_key"] = api_key
    config.set_setting("vision_slot", slot)

    public = {k: v for k, v in slot.items() if k != "api_key"}
    public["key_configured"] = bool(api_key or (key_env and os.environ.get(key_env)))
    return {"ok": True, "configured": public, "vision_chain": _vision_chain_status()}


def vision_status() -> dict[str, Any]:
    return {"ok": True, "vision_chain": _vision_chain_status()}


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
        port=int(payload.get("port") or DEFAULT_PORT),
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
    # 非流式入口没有回传确认卡的通道，所以 approval="remote" 在这里仍然是只读
    # （问不到人就不能写）。只有 approval="auto"（调用方已一次性授权）能开写。
    auto_approval = _approval_mode(payload.get("approval")) == "auto"
    ctx = ToolContext(
        execute=bool(auto_approval and not plan_mode),
        plan_mode=bool(plan_mode),
        workspace=str(payload.get("workspace") or ""),
        task_id=str(payload.get("task_id") or ""),
    )
    if auto_approval and not plan_mode:
        ctx.perm.accept_edits = True
    if isinstance(payload.get("ops_bridge"), dict):
        ctx.ops_bridge = dict(payload.get("ops_bridge") or {})
    if isinstance(payload.get("ops_context"), dict):
        ctx.ops_context = dict(payload.get("ops_context") or {})
    ctx.session_id = _checked_session_id(payload.get("session_id"))
    ctx.turn_id = str(payload.get("turn_id") or "")
    if payload.get("workspace"):
        ctx.workspace_declared = str(payload.get("workspace") or "")
    if payload.get("asin"):
        ctx.asin = str(payload.get("asin") or "")

    messages, created_at, turn_base = _chat_messages(message, payload, ctx)
    events: list[dict[str, Any]] = []

    def narrate(text: str) -> None:
        events.append({"type": "event", "text": security.redact_text(str(text))})

    # 视觉降级发生在 _chat_messages 里（那时 narrate 还没定义），这里补发它的说明。
    for note in (ctx.vision_notes or []):
        narrate(note)

    try:
        provider = provider or build_chain(model_cfg, api_key, narrate=narrate)
        text = agent_loop.run_turn(provider, ctx, messages, max_steps=(_int(payload.get("max_steps"), 0) or None),
                                   narrate=narrate, tools=_tools_for(payload))
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
    if ctx.vision_tier:
        result["vision_tier"] = dict(ctx.vision_tier)
    if ctx.task_id:
        try:
            result["task"] = task_runner.load(ctx.task_id)
        except (FileNotFoundError, OSError, ValueError):
            pass
    if payload.get("persist", True):
        sessions.append_turn(
            ctx.session_id,
            str(messages[0].get("content") or "") if messages else "",
            messages[turn_base:],
            model=model_cfg.get("model", ""),
            usage={},
            created=created_at,
        )
    return result


def chat_stream(payload: dict[str, Any], send: Any, provider: Any | None = None,
                client_gone: "threading.Event | None" = None) -> dict[str, Any]:
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

    # 审批三档（对齐 CLI 的 --permission-mode）：
    #   none   = 只读，写工具一律不落地（默认，老调用方零影响）
    #   remote = 逐项审批，每个写操作弹网页确认卡、等人点
    #   auto   = 完全放行，写操作不再问人（等价 CLI 的 --approve-all / accept_edits）
    # 不传 approval 时下面这几行的结果与改动前逐字一致。
    approval_mode = _approval_mode(payload.get("approval"))
    remote_approval = approval_mode == "remote"
    auto_approval = approval_mode == "auto"
    plan_mode = payload.get("plan_mode")
    if plan_mode is None:
        # 只读仍是默认。开了审批/放行的调用方通常会显式传 plan_mode=false；
        # 没传就仍按只读走，宁可少做也不要在没人看着的时候动线上数据。
        plan_mode = True
    ctx = ToolContext(
        # execute 只在"有人兜底"的前提下打开：逐项审批是"写之前问到人"，
        # 完全放行是"人已经提前一次性授权了这一轮"。两者都不是无声开写。
        execute=bool((remote_approval or auto_approval) and not plan_mode),
        plan_mode=bool(plan_mode),
        workspace=str(payload.get("workspace") or ""),
        task_id=str(payload.get("task_id") or ""),
    )
    if isinstance(payload.get("ops_bridge"), dict):
        ctx.ops_bridge = dict(payload.get("ops_bridge") or {})
    if isinstance(payload.get("ops_context"), dict):
        ctx.ops_context = dict(payload.get("ops_context") or {})
    ctx.session_id = _checked_session_id(payload.get("session_id"))
    ctx.turn_id = str(payload.get("turn_id") or "")
    # 调用方显式给了工作区 = 一条边界，范围锁定只能在里面收窄，不能往上放宽。
    if payload.get("workspace"):
        ctx.workspace_declared = str(payload.get("workspace") or "")
    if payload.get("asin"):
        ctx.asin = str(payload.get("asin") or "")
    if remote_approval:
        ctx.perm.prompt_fn = RemoteApproval(
            send, ctx.session_id, client_gone=client_gone,
            timeout=float(payload.get("approval_timeout") or DEFAULT_APPROVAL_TIMEOUT),
        ).prompt
    if auto_approval:
        # 完全放行 = 本轮所有写操作自动批准，一张确认卡都不弹（CLI 的 --approve-all
        # 走的是同一个开关）。**只在 plan_mode=false 时才有意义**：计划模式下写工具
        # 在更外层就被拦住了，这里放行也落不了地。
        ctx.perm.accept_edits = bool(not plan_mode)

    # 这一轮走哪条路线（闲聊快车道 / 板块直达 / 常规）。判不准一律落 work，
    # 也就是改动前的行为。见 routing.py 顶部那段"慢的是步数不是模型"。
    route = routing.classify(
        message,
        ops_bridge=bool(ctx.ops_bridge),
        has_attachments=bool(payload.get("images") or payload.get("references")),
    )
    if route.is_chat or route.is_board:
        # 闲聊没有阶段可汇报；板块工具本身就是一次长任务、自己会回报进度 ——
        # 这两种情况下 todo + 阶段汇报的状态机只会挡在实际动作前面（实测一句
        # 「测试」18 步里 17 步花在这上面）。
        ctx.progress_reporting_disabled = True

    try:
        messages, created_at, turn_base = _chat_messages(message, payload, ctx, route)
    except ValueError as exc:
        data = {"ok": False, "error": str(exc)}
        send("error", data)
        return data
    send("start", {"ok": True, "session_id": ctx.session_id, "read_only": bool(plan_mode),
                   "approval": approval_mode,
                   "lane": route.lane, "lane_reason": route.reason,
                   "model": health()["model"]})

    # 上下文占用：**在第一个 token 之前就发**。进度条要回答"这轮带了多少东西进去"，
    # 等收尾再说就晚了 —— 那时候用户已经在等回答，看不看进度条都无所谓了。
    turn_tools = _tools_for(payload, route)
    send("context", context.snapshot(messages, turn_tools, model_cfg.get("model", "")))

    # 自动技能匹配：serve 一直只注入知识证据、不选技能（CLI 会）。开了 auto_skill
    # 就用同一套 skills.context_for_query，并把命中结果发给前端画技能芯片。
    # 闲聊路线不选技能：一句问候配一本 1600 字的运营手册，除了把模型往
    # "按手册做审计"带没有别的作用。
    if (payload.get("auto_skill") and not str(payload.get("skill") or "").strip()
            and not route.is_chat):
        matched = _auto_skill_context(message, messages)
        if matched:
            send("skill_match", stream_json.skill_match_event(ctx.session_id, matched))

    def narrate(text: str) -> None:
        send("event", {"type": "event", "text": security.redact_text(str(text))})

    # 视觉降级发生在 _chat_messages 里（narrate 尚未定义），这里补发说明并单发一个
    # vision_tier 事件——前端要靠它画"本轮走了哪一档"的徽标。
    for note in (ctx.vision_notes or []):
        narrate(note)
    if ctx.vision_tier:
        send("vision_tier", dict(ctx.vision_tier))

    # 本轮的执行步骤，按 call_id 收口成"每个调用只留最终态"（running → ok/error 合并，
    # 与前端 mergeStep 同一语义）。轮次收尾时落盘 —— 此前它们只流给前端就扔了，
    # 于是刷新之后"它刚才干了什么"一片空白。
    turn_steps: dict[str, dict] = {}
    turn_skills: list[dict] = []

    def emit(ev: dict) -> None:
        # run_turn_stream 的结构化事件通道原本只喂 CLI 的 stream-json。这里只放行
        # 步骤类事件：assistant/tool_result 的内容前端已经能从 token/final 拿到，
        # 再发一份就是重复。
        kind = str(ev.get("type") or "")
        if kind in ("step", "skill_match", "file_change"):
            send(kind, ev)
        if kind == "step" and ev.get("id"):
            turn_steps[str(ev["id"])] = dict(ev)
        elif kind == "skill_match" and ev.get("skills"):
            turn_skills.append(dict(ev))

    try:
        provider = provider or build_chain(model_cfg, api_key, narrate=narrate)
        out = agent_loop.run_turn_stream(
            provider,
            ctx,
            messages,
            max_steps=(_int(payload.get("max_steps"), 0) or None),
            narrate=narrate,
            emit=emit,
            tools=turn_tools,
            render=lambda text: send("token", {"text": security.redact_text(str(text))}),
            model=model_cfg.get("model", ""),
            # Web 前端以 final.text 为准整体替换气泡：带知识引证也照常流式，
            # 否则命中检索的问题（运营问题几乎全命中）从头到尾一个字不吐。
            # 只累加 token、不认 final 的调用方（IvyeaOps 的报告合成）传 true：
            # 引证门会让模型带着 [K#] 把整篇重写一遍，不 defer 就会收到两份报告。
            defer_citation_text=payload.get("defer_citation_text") is True,
            # 思考流：**必须调用方显式要**，默认一个字都不发。
            #
            # 不是保守，是兼容性硬约束：客户端的事件分发最后一条是"未知事件 → 当成老
            # agent 的自由文本叙述渲染"。默认开的话，装着旧版前端的用户一升级 agent，
            # 满屏就全是模型的思考碎片，而且他没有任何开关能关掉。
            # 与 defer_citation_text 同一路数：新行为 opt-in，老调用方一字不变。
            render_reasoning=(
                (lambda t: send("reasoning", {"text": security.redact_text(str(t))}))
                if payload.get("stream_reasoning") is True else None),
            # 一轮里正文可能被吐好几遍（工具前的开场白、门禁打回后的整篇重写）。
            # 终端叠着看没问题，网页把 token 拼进同一个气泡就成了"同一张表连出
            # 三遍"。这条事件告诉前端：前面那一稿作废，从下一个 token 重新开始。
            on_answer_reset=lambda reason: send(
                "answer_reset", {"reason": str(reason), "session_id": ctx.session_id}),
        )
    except LLMError as exc:
        data = {"ok": False, "error": "model_error", "detail": str(exc)}
        send("error", data)
        return data

    if payload.get("persist", True):
        steps = list(turn_steps.values())
        # 技能命中锚在本轮第一个 call_id 上 —— 详情按轮分页时靠它认出"这批技能属于哪一轮"。
        # 一轮里一个工具都没调时它没有锚点，也就没有执行过程可显示，技能行随之省略。
        anchor = steps[0].get("id") if steps else ""
        skill_rows = ([{"anchor": anchor, "skills": turn_skills[-1].get("skills") or []}]
                      if steps and turn_skills else [])
        sessions.append_turn(
            ctx.session_id,
            str(messages[0].get("content") or "") if messages else "",
            messages[turn_base:],
            model=model_cfg.get("model", ""), usage={}, created=created_at,
            steps=steps, skill_matches=skill_rows)
    data = {
        "ok": True,
        "session_id": ctx.session_id,
        "text": out.get("text", ""),
        "usage": out.get("usage") or {},
        "messages": _public_messages(messages),
        "read_only": bool(plan_mode),
        "todos": list(ctx.todos or []),
        "progress": progress_reporting.public_state(ctx),
        # 收尾再算一次：本轮的工具结果全都留在上下文里了，进度条要走到本轮之后的
        # 真实位置 —— 下一轮就是从这里起步的。
        "context": context.snapshot(messages, turn_tools, model_cfg.get("model", "")),
    }
    if ctx.vision_tier:
        data["vision_tier"] = dict(ctx.vision_tier)
    send("final", data)
    return data


def chat_session_list(limit: int = 20) -> dict[str, Any]:
    return {"ok": True, "sessions": [_public_session(row) for row in sessions.listing(limit=limit)]}


def chat_session_detail(session_id: str, *, turns: int = _DETAIL_TURNS_DEFAULT,
                        before: int | None = None) -> dict[str, Any]:
    data = sessions.load(session_id)
    if not data:
        raise FileNotFoundError(f"会话不存在：{session_id}")
    return {"ok": True, "session": _public_session_detail(data, turns=turns, before=before)}


def chat_session_delete(session_id: str) -> dict[str, Any]:
    if not sessions.delete(session_id):
        raise FileNotFoundError(f"会话不存在：{session_id}")
    return {"ok": True, "deleted": session_id}


def chat_session_create(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = _checked_session_id(payload.get("id"))
    initial = str(payload.get("message") or payload.get("title") or "").strip()
    messages: list[dict[str, Any]] = []
    if initial:
        messages.append({"role": "user", "content": initial})
    sessions.save(session_id, messages, model=config.get_model_config().get("model", ""))
    data = sessions.load(session_id) or {"id": session_id, "messages": messages}
    return {"ok": True, "session": _public_session_detail(data)}


def _checked_session_id(raw: Any) -> str:
    """把调用方给的 session_id 收成安全 id，留空则新生成。

    id 直接拼成文件名，所以非法值必须在入口就打回 —— 拖到 sessions.save 才炸
    就变成 500，调用方只能看到"服务器错误"，查不出是自己传了个越界的 id。
    """
    sid = str(raw or "")
    if not sid:
        return sessions.new_id()
    if not sessions.is_safe_id(sid):
        raise ValueError("invalid session_id")
    return sid


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
    session_id = _checked_session_id(payload.get("id"))
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
            self._json(200, {"ok": True, "retrieval": retrieval.capabilities(),
                             "vision_chain": _vision_chain_status()})
            return
        if parsed.path == "/v1/config/vision":
            self._json(200, vision_status())
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
            # turns/before：按轮分页。不带参数 = 最后几轮，老调用方照常能用，
            # 而且比改动前（末 30 条消息）拿到的提问只多不少。
            before_raw = _first(qs, "before")
            try:
                self._json(200, chat_session_detail(
                    session_id,
                    turns=_int(_first(qs, "turns"), _DETAIL_TURNS_DEFAULT),
                    before=(_int(before_raw, 0) if before_raw not in (None, "") else None)))
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
                # client_gone 传下去，远程审批才知道"页面已经关了，别再等人确认"。
                chat_stream(body, _locked_send, client_gone=client_gone)
            except ValueError as exc:
                # 入参问题（如非法 session_id）。响应头早发出去了，退不回 400，
                # 只能走 error 事件 —— 直接抛会让连接无声断掉，前端只看到"卡住了"。
                _locked_send("error", {"detail": str(exc)})
            finally:
                done.set()
            return
        if parsed.path == "/v1/chat/permission":
            request_id = str(body.get("request_id") or "").strip()
            choice = str(body.get("choice") or "").strip()
            if not request_id or not choice:
                self._json(400, {"ok": False, "error": "request_id 与 choice 必填"})
                return
            ok = resolve_permission(request_id, choice)
            self._json(200 if ok else 404, {
                "ok": ok,
                "request_id": request_id,
                # 过期/未知一律照实说：这一步多半已经超时被拒或轮次已收尾，
                # 前端据此把卡片改成"已失效"，而不是让用户以为点成功了。
                "error": "" if ok else "unknown_or_expired_request",
            })
            return
        if parsed.path == "/v1/chat":
            try:
                self._json(200, chat_run(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/chat/sessions/import":
            try:
                self._json(200, chat_session_import(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/v1/chat/sessions":
            try:
                self._json(200, chat_session_create(body))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
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
        if parsed.path == "/v1/config/vision":
            self._json(200, vision_configure(body))
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


def _tools_for(payload: dict[str, Any], route: "routing.Route | None" = None) -> list | None:
    """工具集：use_tools=false → 不挂任何工具（纯文本生成，模型不会绕去查工具，
    也不会在正文里夹带工具叙述）；默认 None = 全量 TOOL_SCHEMAS。
    IvyeaOps 把 agent 当文本引擎用（报告合成/JSON 抽取）时传 false。

    闲聊路线同样不挂：54 个工具 ≈ 6.9K token 每步重发，还会诱导模型"顺手查一下"，
    白白多走一两个来回。板块任务和常规任务照挂全量 —— 裁工具省的是 token，
    缺能力赔的是整件事做不成。"""
    if payload.get("use_tools") is False:
        return []
    if route is not None and route.is_chat:
        return []
    return None


#: 审批档位的别名 → 规范值。CLI 那边叫 `approve-all`，工作台叫 `auto`，说的是同一档；
#: 认不出来的值一律落 "none"（只读）—— 审批档位判错的方向必须是"少做"。
_APPROVAL_ALIASES = {
    "": "none", "none": "none", "readonly": "none", "read_only": "none", "plan": "none",
    "remote": "remote", "ask": "remote",
    "auto": "auto", "all": "auto", "approve-all": "auto", "approve_all": "auto",
    "accept-edits": "auto", "acceptedits": "auto", "bypass": "auto",
}


def _approval_mode(raw: Any) -> str:
    """把调用方给的 approval 收敛成 none / remote / auto。"""
    return _APPROVAL_ALIASES.get(str(raw or "none").strip().lower(), "none")


def _model_requires_key(settings: dict[str, Any]) -> bool:
    auth = (settings.get("auth_type") or "api_key").lower()
    if auth in ("none", "aws_sdk"):
        return False
    return bool(settings.get("key_env") or auth in ("oauth_external", "oauth_device_code", "copilot"))


def _chat_messages(message: str, payload: dict[str, Any], ctx: ToolContext,
                   route: "routing.Route | None" = None) -> tuple[list[dict[str, Any]], float | None, int]:
    system = agent_loop.SYSTEM_PROMPT + agent_loop.runtime_context_note()
    if ctx.plan_mode:
        system += agent_loop.PLAN_NOTE
    # 这句必须跟着审批档位走。**曾经它是无条件拼上去的** —— 于是用户在界面上选了
    # 「逐项审批」「完全放行」，系统提示词里却还写着"当前默认只读、不要在本轮直接
    # 执行"，模型照着这句话只给方案不动手，看起来就是那两档开关坏了。
    if ctx.plan_mode:
        system += "\n\n[IvyeaOps 嵌入模式] 当前只读。需要写入广告、文件或执行命令时，先输出计划和审批项，不要在本轮直接执行。"
    elif ctx.perm.accept_edits:
        system += ("\n\n[IvyeaOps 嵌入模式] 当前完全放行：用户已经为这一轮授权了写操作，"
                   "该动手就动手，不要再逐条问他要不要执行。但每一次写入前仍要说清"
                   "「改什么、改成什么、影响面」，做完给出可核对的结果。")
    else:
        system += ("\n\n[IvyeaOps 嵌入模式] 当前逐项审批：可以执行写操作，每一次写入会弹确认卡给用户点，"
                   "所以直接调用对应工具即可，不要因为怕改坏而退回「只给方案」。工具参数要写准，"
                   "并在确认卡的说明里讲清这一步会改什么。")
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
    if route is not None and route.is_board:
        user_content += routing.board_hint(route)
    # 闲聊不查知识库：问候语检索不出东西，白跑一趟；万一检索到了，反而是给
    # 「你好」配上几百字亚马逊证据。
    if payload.get("inject_retrieval", True) and not (route is not None and route.is_chat):
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
    # 本轮起点：这之前都是历史，这之后（含这条 user 和后续工具/回答）才是本轮新增。
    # 落盘时只写这一段，见 sessions.append_turn —— 整份覆盖会吃掉并发的另一轮。
    base = len(messages)
    messages.append({"role": "user", "content": _with_payload_images(user_content, payload, ctx)})
    return messages, created_at, base


def _auto_skill_context(message: str, messages: list) -> list[dict[str, Any]]:
    """按用户问题自动匹配 skill、注入本轮 user 消息，返回命中列表供 skill_match 事件用。

    serve 一直只做知识证据注入、不选技能（选技能只有 CLI 会做），于是同一个问题
    在终端和网页会走出两套流程。这里复用 cli.py 那条路上的同一个
    skills.context_for_query 和同一段注入文案，把两边拉齐。

    不另设"是不是亚马逊问题"的闸：skills 库全是亚马逊域，纯代码问题打分为 0
    自然不会命中，多一道判断反而多一处会跟 CLI 走偏的地方。
    """
    try:
        sctx, sids = skills.context_for_query(message, limit=2)
    except Exception:  # noqa: BLE001 — 技能匹配失败绝不该让整轮对话挂掉
        return []
    if not sctx or not sids:
        return []
    note = ("\n\n[Ivyea Skill：本轮相关可复用流程]\n" + sctx
            + "\n\n要求：优先按 skill workflow 组织执行步骤；涉及事实依据时再结合知识库。")
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = content + note
        elif isinstance(content, list):
            # 多模态消息：追加到文本块，图片块原样不动。
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    part["text"] = str(part.get("text") or "") + note
                    break
            else:
                content.insert(0, {"type": "text", "text": note})
        break
    scores = {}
    try:
        scores = {sk.id: score for sk, score in skills.search(message, limit=len(sids))}
    except Exception:  # noqa: BLE001
        pass
    out: list[dict[str, Any]] = []
    for sid in sids:
        sk = skills.get_skill(sid)
        out.append({
            "id": sid,
            "title": (sk.title if sk else sid),
            "domain": (sk.domain if sk else ""),
            "score": scores.get(sid, 0),
        })
    return out


def _with_payload_images(user_content: str, payload: dict[str, Any], ctx: Any = None):
    """可选多模态：payload["images"] 为 data URI 列表时，走**与 CLI 同一条**视觉
    三档降级链（vision.route_images）。

    此前这里是 `raise main_brain_no_vision` —— serve 自己拦掉了带图请求，于是
    IvyeaOps（唯一走 serve 的调用方）在主脑无视觉时整块功能死掉，而同一台机器上
    的 CLI 却有旁路可走。两边必须共用一条链，不要在这里另写降级逻辑。

    T1 返回多模态 list-content（provider 适配器各自转换，codex→input_image、
    anthropic→image block）；T2/T3 返回已注入视觉文本的纯字符串。
    档位写进 ctx.vision_tier，由调用方在 narrate 可用之后发事件并回传。
    """
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        return user_content
    uris = [str(u) for u in images if isinstance(u, str) and u.startswith("data:image/")][:4]
    if not uris:
        return user_content

    from . import config as _config
    from . import vision as _vision

    notes: list[str] = []
    content, kept, tier = _vision.route_images(
        user_content, uris, _config.get_model_config(), notes.append)
    if ctx is not None:
        ctx.vision_tier = tier
        ctx.vision_notes = notes

    if not kept:
        return content
    parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
    for uri in kept:
        parts.append({"type": "image_url", "image_url": {"url": uri}})
    return parts


def _public_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """给人看的那份记录。live 回包和历史详情共用这一个投影 —— 摘门禁注入只改这里，
    任务台/悬浮球/存量会话文件同时干净（见 transcript.strip_injected）。
    先摘再截末 30 条：否则名额会被门禁提示和废稿吃掉。"""
    rows = []
    for msg in transcript.strip_injected(messages):
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


def _detail_message(msg: dict[str, Any]) -> dict[str, Any]:
    """详情里的一条消息。比 live 回包多留两样东西：`tool_calls` 的 id/name 与
    `tool_call_id` —— 它们是把落盘的执行步骤挂回对应轮次的锚点（靠 call_id 对齐，
    不靠下标，所以压缩过、导入过的会话都不会错位）。"""
    role = str(msg.get("role") or "")
    content = msg.get("content")
    if isinstance(content, list):       # 多模态：只回显文本，不吐 base64
        texts = [str(p.get("text") or "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        imgs = sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
        content = "\n".join(t for t in texts if t) + (f"\n[附图 {imgs} 张]" if imgs else "")
    text = security.redact_text(str(content if content is not None else ""))
    if role == "tool" and len(text) > _DETAIL_TOOL_CONTENT_MAX:
        text = text[:_DETAIL_TOOL_CONTENT_MAX] + "…（已截断）"
    row: dict[str, Any] = {"role": role, "content": text}
    calls = msg.get("tool_calls") or []
    if role == "assistant" and calls:
        row["tool_calls"] = [
            {"id": str(c.get("id") or ""),
             "name": str((c.get("function") or {}).get("name") or c.get("name") or "")}
            for c in calls if isinstance(c, dict)
        ]
    if role == "tool" and msg.get("tool_call_id"):
        row["tool_call_id"] = str(msg.get("tool_call_id"))
    return row


def _public_session_detail(data: dict[str, Any], *, turns: int = _DETAIL_TURNS_DEFAULT,
                           before: int | None = None) -> dict[str, Any]:
    kept = transcript.strip_injected(data.get("messages") or [])
    slices = transcript.turn_slices(kept)
    total = len(slices)
    end_turn = total if before is None else max(0, min(int(before), total))
    size = max(1, min(int(turns or _DETAIL_TURNS_DEFAULT), _DETAIL_TURNS_MAX))
    start_turn = max(0, end_turn - size)
    picked = slices[start_turn:end_turn]
    rows = [_detail_message(m) for a, b in picked for m in kept[a:b]
            if m.get("role") in ("user", "assistant", "tool")]

    # 只回本页涉及的步骤。锚点是 call_id：本页的 assistant 消息里出现过的那些。
    call_ids = {c["id"] for r in rows for c in r.get("tool_calls") or [] if c.get("id")}
    steps = [s for s in (data.get("steps") or []) if str(s.get("id") or "") in call_ids]
    skills = [s for s in (data.get("skill_matches") or [])
              if str(s.get("anchor") or "") in call_ids]

    return {
        "id": data.get("id", ""),
        "created": data.get("created"),
        "updated": data.get("updated"),
        "model": data.get("model", ""),
        "usage": data.get("usage") or {},
        "messages": rows,
        "steps": steps,
        "skill_matches": skills,
        "turns": {"total": total, "from": start_turn, "to": end_turn,
                  "has_more": start_turn > 0},
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
