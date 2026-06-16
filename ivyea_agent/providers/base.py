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


def get_provider(provider: str, api_key: str, model: str) -> LLMProvider:
    """工厂：按名字返回 provider 适配器。预留 openai/anthropic 扩展位。"""
    provider = (provider or "").lower()
    if provider == "deepseek":
        from .deepseek import DeepSeekProvider
        return DeepSeekProvider(api_key, model)
    # 预留：openai / anthropic / gemini / ollama —— 同接口实现即可接入。
    raise LLMError(f"暂不支持的 provider: {provider}（P1 已接入: deepseek）")
