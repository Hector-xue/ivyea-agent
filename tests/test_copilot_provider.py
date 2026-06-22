"""GitHub Copilot provider transport."""
from __future__ import annotations


class _Resp:
    status_code = 200
    text = "ok"

    def json(self):
        return {"choices": [{"message": {"content": "hi", "tool_calls": []}}]}


def test_from_settings_builds_copilot_provider(ivyea_home):
    from ivyea_agent.providers import from_settings
    p = from_settings({"kind": "oauth", "api_mode": "copilot_chat_completions",
                       "model": "gpt-4o", "base_url": "https://api.githubcopilot.com"}, "tok")
    assert p.name == "copilot"
    assert p.model == "gpt-4o"


def test_copilot_provider_sends_required_headers(monkeypatch):
    from ivyea_agent.providers.copilot_provider import CopilotProvider
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp()

    from ivyea_agent.providers import openai_compat
    monkeypatch.setattr(openai_compat.httpx, "post", fake_post)
    p = CopilotProvider("copilot-token", "gpt-4o", "https://api.githubcopilot.com")
    out = p.chat([{"role": "user", "content": "hello"}])
    assert out["content"] == "hi"
    assert captured["url"] == "https://api.githubcopilot.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer copilot-token"
    assert captured["headers"]["Copilot-Integration-Id"] == "vscode-chat"
    assert captured["headers"]["Openai-Intent"] == "conversation-edits"
    assert captured["headers"]["x-initiator"] == "agent"


def test_probe_copilot(monkeypatch):
    from ivyea_agent.providers import copilot_provider, openai_compat
    monkeypatch.setattr(openai_compat.httpx, "post", lambda *a, **k: _Resp())
    result = copilot_provider.probe_copilot("copilot-token", model="gpt-4o",
                                           base_url="https://api.githubcopilot.com")
    assert result["ok"] is True
    assert result["model"] == "gpt-4o"
    assert result["content"] == "hi"
