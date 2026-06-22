"""Gemini native provider translation tests."""
from __future__ import annotations

from ivyea_agent.providers import gemini_provider as gp


def test_messages_to_gemini_with_tool_roundtrip_shape():
    system, contents = gp._messages_to_gemini([
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "recall", "arguments": '{"query":"否词"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
    ])
    assert system == "你是助手"
    assert contents[0] == {"role": "user", "parts": [{"text": "查一下"}]}
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["functionCall"]["name"] == "recall"
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "recall"


def test_tools_to_gemini_sanitizes_extra_json_schema_fields():
    tools = gp._tools_to_gemini([{"type": "function", "function": {
        "name": "read_file",
        "description": "读文件",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string", "title": "Path"}},
            "required": ["path"],
        },
    }}])
    decl = tools[0]["functionDeclarations"][0]
    assert decl["name"] == "read_file"
    assert "additionalProperties" not in decl["parameters"]
    assert "title" not in decl["parameters"]["properties"]["path"]


def test_extract_response_text_tool_and_usage():
    out = gp._extract_response({
        "candidates": [{"content": {"parts": [
            {"text": "需要工具"},
            {"functionCall": {"name": "recall", "args": {"query": "acos"}}},
        ]}}],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5},
    })
    assert out["content"] == "需要工具"
    assert out["tool_calls"] == [{"id": "gemini_call_1", "name": "recall", "arguments": {"query": "acos"}}]
    assert out["usage"] == {"prompt_tokens": 12, "completion_tokens": 5}


def test_from_settings_builds_gemini_provider(ivyea_home):
    from ivyea_agent.providers import from_settings
    p = from_settings({"kind": "native", "api_mode": "gemini_native",
                       "model": "gemini-3-pro-preview",
                       "base_url": "https://generativelanguage.googleapis.com/v1beta"}, "key")
    assert p.name == "gemini" and p.model == "gemini-3-pro-preview"


def test_gemini_chat_posts_native_payload(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(gp.httpx, "post", fake_post)
    provider = gp.GeminiProvider("k", "gemini-test", "https://gemini.test/v1beta")
    out = provider.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                        tools=[{"type": "function", "function": {"name": "recall", "parameters": {"type": "object"}}}])
    assert out["content"] == "ok"
    assert seen["url"] == "https://gemini.test/v1beta/models/gemini-test:generateContent"
    assert seen["params"] == {"key": "k"}
    assert seen["json"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert seen["json"]["tools"][0]["functionDeclarations"][0]["name"] == "recall"
