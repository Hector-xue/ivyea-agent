"""配置：~/.ivyea/ 目录、.env 密钥、settings.json。

对标 ~/.hermes/ 的约定。无第三方依赖（自带极简 .env 解析）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

IVYEA_DIR = Path(os.environ.get("IVYEA_HOME", str(Path.home() / ".ivyea")))
ENV_FILE = IVYEA_DIR / ".env"
SETTINGS_FILE = IVYEA_DIR / "settings.json"
MCP_FILE = IVYEA_DIR / "mcp.json"
AUTH_FILE = IVYEA_DIR / "auth.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "provider": "deepseek-chat",  # 选中的模型 id（见 models.py）
    "label": "DeepSeek V3（deepseek-chat）",
    "kind": "openai",             # openai 兼容 / native / login
    "provider_id": "deepseek",
    "api_mode": "chat_completions",
    "auth_type": "api_key",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "key_env": "DEEPSEEK_API_KEY",
    "target_acos": 0.30,          # 默认目标 ACoS（≤毛利率）
    "site": "US",
}

# provider -> 该 provider 读取的环境变量名（API key）
PROVIDER_ENV_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "deepseek-chat": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-api": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "qwen-oauth": "QWEN_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "zai": "ZAI_API_KEY",
    "glm-legacy": "ZHIPU_API_KEY",
    "doubao": "ARK_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "nous": "NOUS_API_KEY",
    "xai": "XAI_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "custom": "CUSTOM_API_KEY",
}


def ensure_dirs() -> None:
    IVYEA_DIR.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    """把 ~/.ivyea/.env 的键值读进 os.environ（不覆盖已存在的）。"""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def load_settings() -> dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return data


def save_settings(settings: dict[str, Any]) -> None:
    ensure_dirs()
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def get_setting(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)


def set_setting(key: str, value: Any) -> None:
    s = load_settings()
    s[key] = value
    save_settings(s)


def get_api_key(provider: str) -> str:
    load_env()
    env_name = PROVIDER_ENV_KEYS.get(provider, "")
    return os.environ.get(env_name, "") if env_name else ""


def get_model_config() -> dict[str, Any]:
    """当前主脑模型配置（供 providers.from_settings 使用）。"""
    s = load_settings()
    return {
        "provider": s.get("provider", "deepseek-chat"),
        "label": s.get("label", s.get("provider", "")),
        "kind": s.get("kind", "openai"),
        "provider_id": s.get("provider_id", s.get("provider", "deepseek-chat")),
        "api_mode": s.get("api_mode", "chat_completions"),
        "auth_type": s.get("auth_type", "api_key"),
        "model": s.get("model", "deepseek-chat"),
        "base_url": s.get("base_url", "https://api.deepseek.com"),
        "key_env": s.get("key_env", "DEEPSEEK_API_KEY"),
    }


def get_active_key() -> str:
    """当前主脑模型对应凭证（从 ~/.ivyea/.env / 环境 / auth.json 读取）。"""
    load_env()
    s = load_settings()
    env_name = s.get("key_env") or PROVIDER_ENV_KEYS.get(s.get("provider", ""), "")
    auth = (s.get("auth_type") or "api_key").lower()
    if auth == "copilot":
        from . import oauth_auth
        return oauth_auth.resolve_copilot_api_token()
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    if auth in ("oauth_external", "oauth_device_code", "copilot"):
        from . import oauth_auth
        provider_id = s.get("provider_id") or s.get("provider") or ""
        return oauth_auth.resolve_provider_token(str(provider_id), str(env_name or ""))
    return ""


def apply_model(entry: dict[str, Any], model: str = "", base_url: str = "") -> None:
    """把 models.py 的一个条目（或自定义）写入 settings。"""
    s = load_settings()
    s["provider"] = entry.get("id", s.get("provider"))
    s["provider_id"] = entry.get("provider_id", entry.get("id", s.get("provider_id", "")))
    s["label"] = entry.get("label", s.get("label"))
    s["kind"] = entry.get("kind", "openai")
    s["api_mode"] = entry.get("api_mode", s.get("api_mode", "chat_completions"))
    s["auth_type"] = entry.get("auth_type", s.get("auth_type", "api_key"))
    s["model"] = model or entry.get("model", s.get("model"))
    s["base_url"] = base_url or entry.get("base", s.get("base_url"))
    s["key_env"] = entry.get("key_env", s.get("key_env"))
    save_settings(s)


def set_env_key(name: str, value: str) -> None:
    """在 ~/.ivyea/.env 写入/更新一个键（保留其它键）。value 为空则删除该键。"""
    ensure_dirs()
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    out, found = [], False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == name:
            found = True
            if value:
                out.append(f"{name}={value}")
            # value 为空则丢弃该行 = 删除
        else:
            out.append(line)
    if value and not found:
        out.append(f"{name}={value}")
    ENV_FILE.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass
    os.environ[name] = value  # 当前进程即时生效


def set_api_key(provider: str, value: str) -> None:
    env_name = PROVIDER_ENV_KEYS.get(provider)
    if not env_name:
        raise ValueError(f"未知 provider: {provider}")
    set_env_key(env_name, value)


# ── MCP 配置 ────────────────────────────────────────────────────────────────

def load_mcp() -> dict[str, Any]:
    if MCP_FILE.exists():
        try:
            return json.loads(MCP_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"mcpServers": {}}


def save_mcp(data: dict[str, Any]) -> None:
    ensure_dirs()
    MCP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        MCP_FILE.chmod(0o600)
    except OSError:
        pass


def mcp_set_server(name: str, spec: dict[str, Any]) -> None:
    data = load_mcp()
    data.setdefault("mcpServers", {})[name] = spec
    save_mcp(data)


def mcp_remove_server(name: str) -> bool:
    data = load_mcp()
    servers = data.setdefault("mcpServers", {})
    if name in servers:
        del servers[name]
        save_mcp(data)
        return True
    return False
