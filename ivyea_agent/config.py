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

DEFAULT_SETTINGS: dict[str, Any] = {
    "provider": "deepseek",      # 主脑模型 provider
    "model": "deepseek-chat",
    "target_acos": 0.30,          # 默认目标 ACoS（≤毛利率）
    "site": "US",
}

# provider -> 该 provider 读取的环境变量名（API key）
PROVIDER_ENV_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
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
