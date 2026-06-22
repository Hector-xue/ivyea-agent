"""GitHub Copilot chat-completions provider."""
from __future__ import annotations

from typing import Any

from .openai_compat import OpenAICompatProvider


class CopilotProvider(OpenAICompatProvider):
    name = "copilot"

    def _headers(self, *, stream: bool = False) -> dict[str, str]:
        headers = super()._headers(stream=stream)
        headers.update({
            "Editor-Version": "vscode/1.104.1",
            "User-Agent": "IvyeaAgent/1.0",
            "Copilot-Integration-Id": "vscode-chat",
            "Openai-Intent": "conversation-edits",
            "x-initiator": "agent",
        })
        return headers


def probe_copilot(api_key: str, *, model: str = "gpt-4o",
                  base_url: str = "https://api.githubcopilot.com",
                  timeout: float = 30.0) -> dict[str, Any]:
    provider = CopilotProvider(api_key, model, base_url)
    result = provider.chat(
        [{"role": "user", "content": "Reply with the single word OK."}],
        temperature=0.0,
        timeout=timeout,
    )
    return {
        "ok": True,
        "model": provider.model,
        "content": str(result.get("content") or "").strip(),
        "usage": result.get("usage") or {},
    }
