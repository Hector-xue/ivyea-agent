"""Gemini Code Assist OAuth provider."""
from __future__ import annotations


class _Resp:
    status_code = 200
    text = "ok"

    def json(self):
        return {
            "response": {
                "candidates": [{
                    "content": {"parts": [
                        {"text": "hello"},
                        {"functionCall": {"name": "lookup", "args": {"q": "ads"}}},
                    ]}
                }],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
            }
        }


def test_from_settings_builds_gemini_code_assist(ivyea_home):
    from ivyea_agent.providers import from_settings
    p = from_settings({"kind": "oauth", "api_mode": "gemini_code_assist",
                       "model": "gemini-3-pro-preview",
                       "base_url": "cloudcode-pa://google"}, "tok")
    assert p.name == "google-gemini-cli"
    assert p.model == "gemini-3-pro-preview"


def test_gemini_code_assist_payload_and_response(monkeypatch):
    from ivyea_agent.providers.gemini_code_assist_provider import GeminiCodeAssistProvider
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp()

    from ivyea_agent.providers import gemini_code_assist_provider as mod
    monkeypatch.setenv("IVYEA_GEMINI_PROJECT_ID", "project-1")
    monkeypatch.setattr(mod.httpx, "post", fake_post)
    p = GeminiCodeAssistProvider("google-token", "gemini-3-pro-preview")
    out = p.chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {
            "name": "lookup",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        }}],
    )
    assert captured["url"] == "https://cloudcode-pa.googleapis.com/v1internal:generateContent"
    assert captured["headers"]["Authorization"] == "Bearer google-token"
    assert captured["json"]["project"] == "project-1"
    assert captured["json"]["model"] == "gemini-3-pro-preview"
    assert captured["json"]["request"]["contents"][0]["role"] == "user"
    assert captured["json"]["request"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert out["content"] == "hello"
    assert out["tool_calls"][0]["name"] == "lookup"
    assert out["tool_calls"][0]["arguments"] == {"q": "ads"}


def test_gemini_code_assist_uses_saved_project(ivyea_home, monkeypatch):
    from ivyea_agent import oauth_auth
    from ivyea_agent.providers.gemini_code_assist_provider import GeminiCodeAssistProvider
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp()

    from ivyea_agent.providers import gemini_code_assist_provider as mod
    oauth_auth.set_google_project_id("saved-project")
    monkeypatch.setattr(mod.httpx, "post", fake_post)
    GeminiCodeAssistProvider("google-token", "gemini-3-pro-preview").chat(
        [{"role": "user", "content": "hi"}],
    )
    assert captured["json"]["project"] == "saved-project"


def test_probe_gemini_code_assist(ivyea_home, monkeypatch):
    from ivyea_agent.providers import gemini_code_assist_provider as mod

    monkeypatch.setenv("IVYEA_GEMINI_PROJECT_ID", "project-1")
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _Resp())
    result = mod.probe_gemini_code_assist("google-token", model="gemini-3-pro-preview")
    assert result["ok"] is True
    assert result["model"] == "gemini-3-pro-preview"
    assert result["project"] == "project-1"
    assert result["content"] == "hello"


def test_gemini_code_assist_http_error_is_diagnosable(monkeypatch):
    from ivyea_agent.providers.gemini_code_assist_provider import (
        GeminiCodeAssistError,
        GeminiCodeAssistProvider,
        diagnose_gemini_code_assist_error,
    )

    class _ErrResp:
        status_code = 404
        text = '{"error":"project not found"}'

    from ivyea_agent.providers import gemini_code_assist_provider as mod
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _ErrResp())
    try:
        GeminiCodeAssistProvider("google-token", "gemini-3-pro-preview").chat(
            [{"role": "user", "content": "hi"}],
        )
    except GeminiCodeAssistError as exc:
        hints = diagnose_gemini_code_assist_error(exc)
    else:
        raise AssertionError("expected GeminiCodeAssistError")
    assert any("project" in hint for hint in hints)
