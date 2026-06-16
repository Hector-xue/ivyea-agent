"""LLM Provider 抽象 —— 多模型统一接口。

P1 只用到「结构化复核」能力（单次/少次调用，无 ReAct 工具循环）。
工具调用循环留给 P2(执行)与对话模式，到时在此接口上扩展 tools 参数。
"""
from __future__ import annotations

from typing import Optional


class LLMError(Exception):
    pass


class LLMProvider:
    """所有模型适配器的统一接口。"""

    name: str = "base"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def complete(self, system: str, user: str, json_mode: bool = False,
                 temperature: float = 0.2, timeout: float = 60.0) -> str:
        """返回模型文本输出（json_mode=True 时尽量返回 JSON 字符串）。"""
        raise NotImplementedError

    def chat(self, messages: list, tools: Optional[list] = None,
             temperature: float = 0.3, timeout: float = 120.0) -> dict:
        """工具调用对话。messages 为 OpenAI 格式；tools 为 function 列表。
        返回助手消息 dict：{role, content, tool_calls}，其中 tool_calls 为
        [{id, name, arguments(dict)}]（无则空列表）。供对话式 Agent 循环使用。"""
        raise NotImplementedError


def from_settings(model_cfg: dict, api_key: str) -> LLMProvider:
    """按模型配置(models.py 条目/settings)构造 provider。

    kind=openai → 通用 OpenAI 兼容（OpenAI/DeepSeek/通义/Kimi/GLM/豆包/MiniMax/
    OpenRouter/自定义，现已可用）。native(Anthropic/Gemini)/login(Codex/Claude订阅)
    给清晰"规划中"提示，不假装可用。"""
    kind = (model_cfg.get("kind") or "openai").lower()
    model = model_cfg.get("model") or "deepseek-chat"
    base_url = model_cfg.get("base_url") or model_cfg.get("base") or "https://api.deepseek.com"
    if kind == "openai":
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(api_key, model, base_url)
    if kind == "native":
        raise LLMError(f"{model_cfg.get('label', model)} 走厂商原生 API，适配规划中；"
                       "当前可用：OpenAI 兼容类（OpenAI/DeepSeek/通义/Kimi/GLM/豆包/MiniMax/OpenRouter/自定义）。")
    if kind == "login":
        raise LLMError(f"{model_cfg.get('label', model)} 为登录制（免 API key），登录接入规划中。")
    raise LLMError(f"未知模型类型 kind={kind}")
