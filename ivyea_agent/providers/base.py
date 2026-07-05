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
    reasoning_effort: str = "auto"   # off|low|medium|high|auto；由 from_settings 按全局设置注入，各 provider 映射成自家思考参数

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

    def stream_chat(self, messages: list, tools: Optional[list] = None,
                    temperature: float = 0.3, timeout: float = 120.0):
        """流式工具调用，产出事件流（{type:text,...} 增量 + {type:final,...}）。
        默认回退到非流式 chat（不支持流式的 provider 也能用，只是无逐字效果）。"""
        msg = self.chat(messages, tools=tools, temperature=temperature, timeout=timeout)
        content = msg.get("content") or ""
        if content:
            yield {"type": "text", "text": content}
        yield {"type": "final", "content": content,
               "tool_calls": msg.get("tool_calls") or [], "usage": msg.get("usage") or {}}


def from_settings(model_cfg: dict, api_key: str) -> LLMProvider:
    """按模型配置构造 provider，并注入全局 reasoning_effort（思考深度旋钮）。
    provider 由 _build_provider 造好后统一挂 .reasoning_effort，各 provider 在
    请求体里映射成自家思考参数（推理型模型才生效，非推理型 no-op）。"""
    provider = _build_provider(model_cfg, api_key)
    try:
        from .. import config
        eff = str(config.get_setting("reasoning_effort", "high") or "high").lower()
        if eff in ("off", "low", "medium", "high", "auto"):
            provider.reasoning_effort = eff
    except Exception:   # noqa: BLE001  设置读不到不影响主流程
        pass
    return provider


def _build_provider(model_cfg: dict, api_key: str) -> LLMProvider:
    """按模型配置(models.py 条目/settings)构造 provider。

    kind=openai → 通用 OpenAI 兼容（OpenAI/DeepSeek/通义/Kimi/GLM/豆包/MiniMax/
    OpenRouter/自定义，现已可用）。anthropic → Claude 原生。native/oauth 等
    给清晰提示，不假装可用。"""
    kind = (model_cfg.get("kind") or "openai").lower()
    model = model_cfg.get("model") or "deepseek-chat"
    base_url = model_cfg.get("base_url") or model_cfg.get("base") or "https://api.deepseek.com"
    api_mode = (model_cfg.get("api_mode") or "").lower()
    if api_mode == "gemini_native":
        from .gemini_provider import GeminiProvider
        return GeminiProvider(api_key, model, base_url)
    if api_mode == "gemini_code_assist":
        from .gemini_code_assist_provider import GeminiCodeAssistProvider
        return GeminiCodeAssistProvider(api_key, model, base_url)
    if api_mode == "bedrock_converse":
        from .bedrock_provider import BedrockProvider
        return BedrockProvider(api_key, model, base_url)
    if api_mode == "copilot_chat_completions":
        from .copilot_provider import CopilotProvider
        return CopilotProvider(api_key, model, base_url)
    if api_mode == "codex_responses":
        from .codex_provider import CodexProvider
        return CodexProvider(api_key, model, base_url)
    if kind == "openai":
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(api_key, model, base_url)
    if kind == "anthropic":
        from .anthropic_provider import AnthropicProvider
        # 只认 anthropic 自有网关；忽略切换残留的他家 base_url（默认走 api.anthropic.com）
        raw = (model_cfg.get("base_url") or model_cfg.get("base") or "")
        gw = raw if "anthropic" in raw.lower() else ""
        # 订阅版 OAuth：api_mode=anthropic_oauth 或 auth_type=oauth_external → Bearer + oauth beta 头
        oauth = api_mode == "anthropic_oauth" or (model_cfg.get("auth_type") or "").lower() == "oauth_external"
        return AnthropicProvider(api_key, model, gw, oauth=oauth)
    if kind == "native":
        raise LLMError(f"{model_cfg.get('label', model)} 走厂商原生 API，适配规划中；"
                       "当前可用：Claude 原生 + Gemini 原生 + Bedrock Converse + OpenAI 兼容类（OpenAI/DeepSeek/通义/Kimi/GLM/豆包/MiniMax/OpenRouter/本地/自定义）。")
    if kind in ("oauth", "login"):
        auth_type = model_cfg.get("auth_type") or kind
        raise LLMError(f"{model_cfg.get('label', model)} 需要 {auth_type} 认证/专用 transport，尚未接入；"
                       "不要按普通会员网页登录理解。当前可用：API key、OpenAI 兼容端点、本地 Ollama、自定义网关。")
    raise LLMError(f"未知模型类型 kind={kind}")
