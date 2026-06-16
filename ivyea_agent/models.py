"""主流模型清单（国内 + 国外 + 登录制），供 `ivyea model` / `/model` 选择。

kind:
  openai   —— OpenAI 兼容端点（key 鉴权）。OpenAI 官方/DeepSeek/通义/Kimi/GLM/豆包/
              MiniMax/OpenRouter/自定义 都走这条，现已可用。
  native   —— 厂商原生 API（Anthropic/Gemini），适配规划中。
  login    —— 登录制（Codex 用 ChatGPT 会员、Claude 订阅），免 API key，接入规划中。
"""
from __future__ import annotations

from typing import Any, Optional

# (id, 标签, kind, model, base_url, key_env, note)
MODELS: list[dict[str, Any]] = [
    # ── 国外（OpenAI 兼容，可用）──
    {"id": "gpt-4o",          "label": "OpenAI GPT-4o",            "kind": "openai", "model": "gpt-4o",              "base": "https://api.openai.com/v1",  "key_env": "OPENAI_API_KEY",  "group": "国外"},
    {"id": "gpt-4o-mini",     "label": "OpenAI GPT-4o mini（便宜）", "kind": "openai", "model": "gpt-4o-mini",         "base": "https://api.openai.com/v1",  "key_env": "OPENAI_API_KEY",  "group": "国外"},
    # ── 国外（原生 API，规划中）──
    {"id": "claude-sonnet",   "label": "Anthropic Claude Sonnet",  "kind": "native", "model": "claude-3-7-sonnet-latest", "key_env": "ANTHROPIC_API_KEY", "group": "国外", "note": "原生 API 适配规划中"},
    {"id": "gemini",          "label": "Google Gemini 2.5",        "kind": "native", "model": "gemini-2.5-pro",      "key_env": "GEMINI_API_KEY",  "group": "国外", "note": "原生 API 适配规划中"},
    # ── 国内（OpenAI 兼容，可用）──
    {"id": "deepseek-chat",   "label": "DeepSeek V3（deepseek-chat）", "kind": "openai", "model": "deepseek-chat",     "base": "https://api.deepseek.com",   "key_env": "DEEPSEEK_API_KEY", "group": "国内"},
    {"id": "deepseek-reasoner", "label": "DeepSeek R1（reasoner）",  "kind": "openai", "model": "deepseek-reasoner",   "base": "https://api.deepseek.com",   "key_env": "DEEPSEEK_API_KEY", "group": "国内"},
    {"id": "qwen-max",        "label": "通义千问 Qwen-Max",         "kind": "openai", "model": "qwen-max",            "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key_env": "DASHSCOPE_API_KEY", "group": "国内"},
    {"id": "kimi",            "label": "Kimi / Moonshot",          "kind": "openai", "model": "moonshot-v1-8k",      "base": "https://api.moonshot.cn/v1", "key_env": "MOONSHOT_API_KEY", "group": "国内"},
    {"id": "glm-4",           "label": "智谱 GLM-4",                "kind": "openai", "model": "glm-4-plus",          "base": "https://open.bigmodel.cn/api/paas/v4", "key_env": "ZHIPU_API_KEY", "group": "国内"},
    {"id": "doubao",          "label": "字节 豆包 Doubao",          "kind": "openai", "model": "doubao-pro-32k",      "base": "https://ark.cn-beijing.volces.com/api/v3", "key_env": "ARK_API_KEY", "group": "国内"},
    {"id": "minimax",         "label": "MiniMax abab",             "kind": "openai", "model": "abab6.5s-chat",       "base": "https://api.minimax.chat/v1", "key_env": "MINIMAX_API_KEY", "group": "国内"},
    # ── 聚合 / 自定义 ──
    {"id": "openrouter",      "label": "OpenRouter（任意模型）",     "kind": "openai", "model": "openai/gpt-4o-mini",  "base": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "group": "聚合/自定义"},
    {"id": "custom",          "label": "自定义 OpenAI 兼容端点",     "kind": "openai", "model": "",                    "base": "",  "key_env": "CUSTOM_API_KEY", "group": "聚合/自定义", "note": "自填 base_url / model / key"},
    # ── 登录制（免 key，规划中）──
    {"id": "codex",           "label": "Codex（ChatGPT 会员登录）",  "kind": "login",  "model": "gpt-5-codex",         "key_env": "", "group": "登录制", "note": "用 ChatGPT 会员账号登录、免 API key — 登录接入规划中"},
    {"id": "claude-sub",      "label": "Claude（订阅登录）",         "kind": "login",  "model": "claude",              "key_env": "", "group": "登录制", "note": "Claude 订阅登录、免 API key — 登录接入规划中"},
]

GROUP_ORDER = ["国外", "国内", "聚合/自定义", "登录制"]


def by_id(mid: str) -> Optional[dict]:
    for m in MODELS:
        if m["id"] == mid:
            return m
    return None


def grouped() -> list[tuple[str, list[dict]]]:
    return [(g, [m for m in MODELS if m["group"] == g]) for g in GROUP_ORDER]
