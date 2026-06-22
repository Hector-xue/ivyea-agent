"""Model/provider catalog for `ivyea model`.

Hermes' useful pattern is a provider profile registry: auth type, endpoint,
fallback models, aliases, and live model discovery live with the provider
instead of being scattered through the CLI.  Ivyea keeps the implementation
lighter, but follows the same shape so adding providers no longer means
hard-coding one more special case in the picker.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional


PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "label": "OpenAI API",
        "group": "国外 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "signup_url": "https://platform.openai.com/api-keys",
        "models": ["gpt-4.1", "gpt-4o", "gpt-4o-mini"],
        "default_model": "gpt-4o",
        "status": "usable",
    },
    {
        "id": "anthropic",
        "label": "Anthropic Claude API",
        "group": "国外 API",
        "kind": "anthropic",
        "api_mode": "anthropic_messages",
        "auth_type": "api_key",
        "base": "https://api.anthropic.com",
        "models_url": "https://api.anthropic.com/v1/models",
        "key_env": "ANTHROPIC_API_KEY",
        "signup_url": "https://console.anthropic.com/settings/keys",
        "models": [
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-sonnet-4-20250514",
        ],
        "default_model": "claude-sonnet-4-6",
        "status": "usable",
    },
    {
        "id": "gemini",
        "label": "Google Gemini API",
        "group": "国外 API",
        "kind": "native",
        "api_mode": "gemini_native",
        "auth_type": "api_key",
        "base": "https://generativelanguage.googleapis.com/v1beta",
        "key_env": "GEMINI_API_KEY",
        "signup_url": "https://aistudio.google.com/app/apikey",
        "models": ["gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-3.5-flash", "gemini-2.5-pro"],
        "default_model": "gemini-3.1-pro-preview",
        "status": "usable",
        "note": "聊天主脑走 Gemini native generateContent；视觉命令也支持 Gemini 调用。",
    },
    {
        "id": "google-gemini-cli",
        "label": "Gemini Code Assist OAuth",
        "group": "OAuth / 订阅",
        "kind": "oauth",
        "api_mode": "gemini_code_assist",
        "auth_type": "oauth_external",
        "base": "cloudcode-pa://google",
        "key_env": "",
        "models": ["gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-3-flash-preview"],
        "default_model": "gemini-3-pro-preview",
        "status": "usable",
        "note": "使用 Google OAuth token 调用 cloudcode-pa Code Assist；支持浏览器登录、手动粘贴模式、refresh token 和 project 保存。",
    },
    {
        "id": "openai-codex",
        "label": "OpenAI Codex OAuth",
        "group": "OAuth / 订阅",
        "kind": "oauth",
        "api_mode": "codex_responses",
        "auth_type": "oauth_external",
        "base": "https://chatgpt.com/backend-api/codex",
        "key_env": "",
        "models": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5-codex"],
        "default_model": "gpt-5.5",
        "status": "usable",
        "note": "Codex OAuth device-code 登录后走 chatgpt.com/backend-api/codex Responses；支持 Responses streaming 和工具调用。",
    },
    {
        "id": "copilot",
        "label": "GitHub Copilot / GitHub Models",
        "group": "OAuth / 订阅",
        "kind": "oauth",
        "api_mode": "copilot_chat_completions",
        "auth_type": "copilot",
        "base": "https://api.githubcopilot.com",
        "key_env": "GITHUB_TOKEN",
        "models": ["gpt-4o", "gpt-4.1", "claude-sonnet-4-6", "gemini-3-pro-preview"],
        "default_model": "gpt-4o",
        "status": "usable",
        "note": "使用 GitHub token 换取短期 Copilot API token 后走 Copilot chat/completions；classic PAT(ghp_*) 不支持。",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "group": "国内 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY",
        "signup_url": "https://platform.deepseek.com/",
        "models": ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro", "deepseek-v4-flash"],
        "default_model": "deepseek-chat",
        "status": "usable",
    },
    {
        "id": "qwen",
        "label": "通义千问 / DashScope",
        "group": "国内 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "models": ["qwen-max", "qwen-plus", "qwen3-coder-plus", "qwen3-coder-next"],
        "default_model": "qwen-max",
        "status": "usable",
    },
    {
        "id": "qwen-oauth",
        "label": "Qwen OAuth / Portal",
        "group": "OAuth / 订阅",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "oauth_external",
        "base": "https://portal.qwen.ai/v1",
        "key_env": "QWEN_API_KEY",
        "models": ["qwen3.7-max", "qwen3-coder-next", "qwen3-coder-plus"],
        "default_model": "qwen3.7-max",
        "status": "usable",
        "note": "可用 QWEN_API_KEY，或用 Qwen CLI 登录导入/手动 token 存本地 Bearer token；支持 refresh token 自动刷新。",
    },
    {
        "id": "kimi",
        "label": "Kimi / Moonshot",
        "group": "国内 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://api.moonshot.cn/v1",
        "key_env": "MOONSHOT_API_KEY",
        "models": ["moonshot-v1-8k", "kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking"],
        "default_model": "moonshot-v1-8k",
        "status": "usable",
    },
    {
        "id": "kimi-coding",
        "label": "Kimi Coding",
        "group": "国内 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://api.moonshot.ai/v1",
        "key_env": "KIMI_API_KEY",
        "models": ["kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking", "kimi-for-coding"],
        "default_model": "kimi-k2.6",
        "status": "usable",
        "note": "sk-kimi-* 的 Anthropic-compatible /coding 形态后续单独接；普通 key 走 OpenAI 兼容。",
    },
    {
        "id": "zai",
        "label": "Z.AI / 智谱 GLM",
        "group": "国内 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://api.z.ai/api/paas/v4",
        "key_env": "ZAI_API_KEY",
        "models": ["glm-5.1", "glm-5", "glm-4.7", "glm-4.5"],
        "default_model": "glm-5",
        "status": "usable",
    },
    {
        "id": "glm-legacy",
        "label": "智谱 GLM legacy",
        "group": "国内 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": "ZHIPU_API_KEY",
        "models": ["glm-4-plus", "glm-4-flash"],
        "default_model": "glm-4-plus",
        "status": "usable",
    },
    {
        "id": "doubao",
        "label": "字节豆包 / Volcano Ark",
        "group": "国内 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://ark.cn-beijing.volces.com/api/v3",
        "key_env": "ARK_API_KEY",
        "models": ["doubao-pro-32k", "doubao-seed-1-6"],
        "default_model": "doubao-pro-32k",
        "status": "usable",
        "note": "豆包实际 model/deployment 名通常由火山控制台生成，可选择后覆盖。",
    },
    {
        "id": "minimax",
        "label": "MiniMax",
        "group": "国内 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://api.minimax.chat/v1",
        "key_env": "MINIMAX_API_KEY",
        "models": ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5", "abab6.5s-chat"],
        "default_model": "MiniMax-M3",
        "status": "usable",
        "note": "Hermes 使用 MiniMax anthropic endpoint；Ivyea 当前先走 OpenAI 兼容。",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "group": "聚合 / 路由",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://openrouter.ai/api/v1",
        "models_url": "https://openrouter.ai/api/v1/models",
        "key_env": "OPENROUTER_API_KEY",
        "signup_url": "https://openrouter.ai/keys",
        "models": [
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-4o",
            "google/gemini-3-pro-preview",
            "deepseek/deepseek-chat",
            "moonshotai/kimi-k2.6",
            "minimax/minimax-m3",
            "z-ai/glm-5.1",
        ],
        "default_model": "anthropic/claude-sonnet-4.6",
        "status": "usable",
    },
    {
        "id": "nous",
        "label": "Nous Portal",
        "group": "聚合 / 路由",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://inference.nousresearch.com/v1",
        "key_env": "NOUS_API_KEY",
        "models": ["hermes-3-405b", "hermes-3-70b", "anthropic/claude-sonnet-4.6"],
        "default_model": "hermes-3-405b",
        "status": "usable",
        "note": "当前按 NOUS_API_KEY 使用；Portal device-code 登录后续接入。",
    },
    {
        "id": "xai",
        "label": "xAI / Grok",
        "group": "国外 API",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "https://api.x.ai/v1",
        "key_env": "XAI_API_KEY",
        "models": ["grok-4.3", "grok-code-fast-1"],
        "default_model": "grok-4.3",
        "status": "usable",
    },
    {
        "id": "ollama",
        "label": "Ollama / Local OpenAI-compatible",
        "group": "本地 / 自定义",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "none",
        "base": "http://localhost:11434/v1",
        "key_env": "OLLAMA_API_KEY",
        "models": ["qwen3-coder", "deepseek-r1", "llama3.1"],
        "default_model": "qwen3-coder",
        "status": "usable",
        "note": "本地 Ollama 通常不需要 key；若服务要求鉴权可设置 OLLAMA_API_KEY。",
    },
    {
        "id": "bedrock",
        "label": "AWS Bedrock",
        "group": "云厂商",
        "kind": "native",
        "api_mode": "bedrock_converse",
        "auth_type": "aws_sdk",
        "base": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "key_env": "",
        "models": ["us.anthropic.claude-sonnet-4-6", "us.amazon.nova-pro-v1:0", "deepseek.v3.2"],
        "default_model": "us.anthropic.claude-sonnet-4-6",
        "status": "usable",
        "note": "需要可选依赖 boto3 和 AWS 默认凭据链。",
    },
    {
        "id": "azure-foundry",
        "label": "Azure AI Foundry",
        "group": "云厂商",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "",
        "key_env": "AZURE_FOUNDRY_API_KEY",
        "models": [],
        "default_model": "",
        "status": "usable",
        "note": "每个 Azure 资源 endpoint/model 不同，选择 custom 或此 provider 后填写 base_url/model。",
    },
    {
        "id": "custom",
        "label": "自定义 OpenAI 兼容端点",
        "group": "本地 / 自定义",
        "kind": "openai",
        "api_mode": "chat_completions",
        "auth_type": "api_key",
        "base": "",
        "key_env": "CUSTOM_API_KEY",
        "models": [],
        "default_model": "",
        "status": "usable",
        "note": "适合 vLLM、LiteLLM、llama.cpp、企业网关、自建代理。",
    },
]

GROUP_ORDER = ["国外 API", "国内 API", "聚合 / 路由", "OAuth / 订阅", "云厂商", "本地 / 自定义"]


def providers() -> list[dict[str, Any]]:
    return list(PROVIDERS)


def provider_by_id(pid: str) -> Optional[dict[str, Any]]:
    for p in PROVIDERS:
        if p["id"] == pid:
            return p
    return None


def model_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        for model in provider.get("models") or []:
            entry = {
                "id": f"{provider['id']}:{model}",
                "provider_id": provider["id"],
                "label": f"{provider['label']} · {model}",
                "kind": provider["kind"],
                "api_mode": provider.get("api_mode", ""),
                "model": model,
                "base": provider.get("base", ""),
                "key_env": provider.get("key_env", ""),
                "group": provider["group"],
                "auth_type": provider.get("auth_type", "api_key"),
                "status": provider.get("status", "usable"),
                "note": provider.get("note", ""),
            }
            entries.append(entry)
    # Back-compatible aliases used by existing docs/tests/users.
    aliases = {
        "gpt-4o": ("openai", "gpt-4o"),
        "gpt-4o-mini": ("openai", "gpt-4o-mini"),
        "claude-opus": ("anthropic", "claude-opus-4-8"),
        "claude-sonnet": ("anthropic", "claude-sonnet-4-6"),
        "claude-haiku": ("anthropic", "claude-haiku-4-5"),
        "gemini": ("gemini", "gemini-3.1-pro-preview"),
        "deepseek-chat": ("deepseek", "deepseek-chat"),
        "deepseek-reasoner": ("deepseek", "deepseek-reasoner"),
        "qwen-max": ("qwen", "qwen-max"),
        "kimi": ("kimi", "moonshot-v1-8k"),
        "glm-4": ("glm-legacy", "glm-4-plus"),
        "doubao": ("doubao", "doubao-pro-32k"),
        "minimax": ("minimax", "MiniMax-M3"),
        "openrouter": ("openrouter", "anthropic/claude-sonnet-4.6"),
        "custom": ("custom", ""),
    }
    by_pair = {(e["provider_id"], e["model"]): e for e in entries}
    for alias, pair in aliases.items():
        if pair in by_pair:
            base = dict(by_pair[pair])
        else:
            provider = provider_by_id(pair[0]) or {}
            base = {
                "provider_id": pair[0],
                "label": provider.get("label", pair[0]),
                "kind": provider.get("kind", "openai"),
                "api_mode": provider.get("api_mode", "chat_completions"),
                "model": pair[1],
                "base": provider.get("base", ""),
                "key_env": provider.get("key_env", ""),
                "group": provider.get("group", "本地 / 自定义"),
                "auth_type": provider.get("auth_type", "api_key"),
                "status": provider.get("status", "usable"),
                "note": provider.get("note", ""),
            }
        base["id"] = alias
        base["label"] = base["label"].replace(" · ", " ")
        entries.append(base)
    return entries


MODELS: list[dict[str, Any]] = model_entries()


def by_id(mid: str) -> Optional[dict[str, Any]]:
    for m in MODELS:
        if m["id"] == mid:
            return m
    if ":" in mid:
        provider_id, model = mid.split(":", 1)
        provider = provider_by_id(provider_id)
        if provider:
            return {
                "id": mid,
                "provider_id": provider_id,
                "label": f"{provider['label']} · {model}",
                "kind": provider["kind"],
                "api_mode": provider.get("api_mode", ""),
                "model": model,
                "base": provider.get("base", ""),
                "key_env": provider.get("key_env", ""),
                "group": provider.get("group", "本地 / 自定义"),
                "auth_type": provider.get("auth_type", "api_key"),
                "status": provider.get("status", "usable"),
                "note": provider.get("note", ""),
            }
    return None


def grouped() -> list[tuple[str, list[dict]]]:
    return [(g, [m for m in MODELS if m["group"] == g]) for g in GROUP_ORDER]


def grouped_providers() -> list[tuple[str, list[dict]]]:
    return [(g, [p for p in PROVIDERS if p["group"] == g]) for g in GROUP_ORDER]


def key_status(provider: dict[str, Any], *, environ: dict[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    auth = provider.get("auth_type", "api_key")
    key_env = provider.get("key_env", "")
    if auth in ("none", "aws_sdk"):
        return auth
    if auth in ("oauth_external", "oauth_device_code", "copilot"):
        if key_env and env.get(key_env):
            return f"configured:{key_env}"
        if auth == "copilot":
            for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
                if env.get(name):
                    return f"configured:{name}"
        try:
            from . import oauth_auth
            token_state = oauth_auth.token_status(str(provider.get("id", "")))
        except (ImportError, OSError):
            token_state = "not-authenticated"
        if token_state != "not-authenticated":
            return token_state
        return f"missing:{key_env or 'auth-token'}"
    if not key_env:
        return "oauth" if "oauth" in auth or auth == "copilot" else "no-key-env"
    return "configured" if env.get(key_env) else f"missing:{key_env}"


def live_models(provider: dict[str, Any], api_key: str = "", timeout: float = 6.0) -> list[str] | None:
    """Fetch a provider model catalog when it exposes an OpenAI-compatible /models endpoint."""
    url = (provider.get("models_url") or "").strip()
    base = (provider.get("base") or "").strip()
    api_mode = provider.get("api_mode", "")
    if not url and api_mode == "gemini_native" and base:
        url = base.rstrip("/") + "/models"
    if not url and base and provider.get("kind") == "openai":
        url = base.rstrip("/") + "/models"
    if not url:
        return None
    if api_mode == "gemini_native" and api_key:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={api_key}"
    req = urllib.request.Request(url)
    if api_key and api_mode == "anthropic_messages":
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
    elif api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "ivyea-agent")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    items = data if isinstance(data, list) else data.get("data", [])
    if not items and isinstance(data, dict):
        items = data.get("models", [])
    ids = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("id") or item.get("name")
        if not raw:
            continue
        model_id = str(raw)
        if model_id.startswith("models/"):
            model_id = model_id.split("/", 1)[1]
        ids.append(model_id)
    return ids or None


def _cache_file():
    from . import config
    return config.IVYEA_DIR / "model_catalog_cache.json"


def _cache_key(provider: dict[str, Any]) -> str:
    return "|".join([
        str(provider.get("id") or ""),
        str(provider.get("models_url") or ""),
        str(provider.get("base") or ""),
    ])


def _load_model_cache() -> dict[str, Any]:
    path = _cache_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_model_cache(data: dict[str, Any]) -> None:
    from . import config
    config.ensure_dirs()
    _cache_file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def provider_models(provider: dict[str, Any], api_key: str = "", *,
                    refresh: bool = False, ttl: float = 24 * 3600) -> tuple[list[str], str]:
    """Return provider models plus source: live/cache/builtin.

    OpenAI-compatible providers often expose /models. For OAuth/private transports
    that do not expose a stable catalog, callers get the curated builtin list.
    """
    builtin = list(provider.get("models") or [])
    if provider.get("kind") != "openai" and not provider.get("models_url") and provider.get("api_mode") != "gemini_native":
        return builtin, "builtin"
    key = _cache_key(provider)
    cache = _load_model_cache()
    row = cache.get(key) if isinstance(cache.get(key), dict) else {}
    now = time.time()
    cached_models = [str(m) for m in row.get("models", []) if m] if row else []
    age = now - float(row.get("ts") or 0) if row else ttl + 1
    if cached_models and not refresh and age <= ttl:
        return cached_models, "cache"
    live = live_models(provider, api_key=api_key)
    if live:
        cache[key] = {"ts": now, "models": live}
        _save_model_cache(cache)
        return live, "live"
    if cached_models:
        return cached_models, "cache"
    return builtin, "builtin"
