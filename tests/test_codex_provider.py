"""OpenAI Codex OAuth Responses provider."""
from __future__ import annotations


class _Resp:
    status_code = 200
    text = "ok"

    def json(self):
        return {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
                {"type": "function_call", "call_id": "call_1", "name": "read_file",
                 "arguments": '{"path":"README.md"}'},
            ]
        }


def test_from_settings_builds_codex_provider(ivyea_home):
    from ivyea_agent.providers import from_settings
    p = from_settings({"kind": "oauth", "api_mode": "codex_responses",
                       "model": "gpt-5.3-codex",
                       "base_url": "https://chatgpt.com/backend-api/codex"}, "tok")
    assert p.name == "openai-codex"
    assert p.model == "gpt-5.3-codex"


def test_codex_provider_payload_and_response(monkeypatch):
    from ivyea_agent.providers.codex_provider import CodexProvider
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp()

    from ivyea_agent.providers import codex_provider
    monkeypatch.setattr(codex_provider.httpx, "post", fake_post)
    p = CodexProvider("codex-token", "gpt-5.3-codex", "https://chatgpt.com/backend-api/codex")
    out = p.chat(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [
                {"id": "call_0", "function": {"name": "list_dir", "arguments": '{"path":"."}'}}
            ]},
            {"role": "tool", "tool_call_id": "call_0", "content": "ok"},
        ],
        tools=[{"type": "function", "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }}],
    )
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["Authorization"] == "Bearer codex-token"
    assert captured["headers"]["originator"] == "codex_cli_rs"
    assert captured["json"]["instructions"] == "sys"
    assert captured["json"]["input"][0]["role"] == "user"
    assert captured["json"]["input"][1]["type"] == "function_call"
    assert captured["json"]["input"][2]["type"] == "function_call_output"
    assert captured["json"]["tools"][0]["name"] == "read_file"
    assert out["content"] == "hello"
    assert out["tool_calls"][0]["name"] == "read_file"
    assert out["tool_calls"][0]["arguments"] == {"path": "README.md"}


def test_parse_codex_responses_sse():
    from ivyea_agent.providers.codex_provider import parse_responses_sse
    lines = [
        'data: {"type":"response.output_text.delta","delta":"he"}',
        'data: {"type":"response.output_text.delta","delta":"llo"}',
        'data: {"type":"response.output_item.added","output_index":1,"item":{"type":"function_call","call_id":"call_1","name":"read_file"}}',
        'data: {"type":"response.function_call_arguments.delta","output_index":1,"delta":"{\\"path\\""}',
        'data: {"type":"response.function_call_arguments.delta","output_index":1,"delta":":\\"README.md\\"}"}',
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":3}}}',
    ]
    events = list(parse_responses_sse(lines))
    assert events[0] == {"type": "text", "text": "he"}
    assert events[1] == {"type": "text", "text": "llo"}
    assert events[-1]["content"] == "hello"
    assert events[-1]["tool_calls"][0]["name"] == "read_file"
    assert events[-1]["tool_calls"][0]["arguments"] == {"path": "README.md"}
    assert events[-1]["usage"] == {"input_tokens": 2, "output_tokens": 3}


def test_codex_provider_stream_chat(monkeypatch):
    from ivyea_agent.providers.codex_provider import CodexProvider
    captured = {}

    class _StreamResp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self):
            return iter([
                'data: {"type":"response.output_text.delta","delta":"hi"}',
                'data: {"type":"response.completed","response":{"usage":{"total_tokens":1}}}',
            ])

    def fake_stream(method, url, headers=None, json=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _StreamResp()

    from ivyea_agent.providers import codex_provider
    monkeypatch.setattr(codex_provider.httpx, "stream", fake_stream)
    p = CodexProvider("codex-token", "gpt-5.3-codex", "https://chatgpt.com/backend-api/codex")
    events = list(p.stream_chat([{"role": "user", "content": "hi"}]))
    assert captured["method"] == "POST"
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["json"]["stream"] is True
    assert captured["headers"]["Authorization"] == "Bearer codex-token"
    assert events[0] == {"type": "text", "text": "hi"}
    assert events[-1]["content"] == "hi"
    assert events[-1]["usage"] == {"total_tokens": 1}


def test_probe_codex(monkeypatch):
    from ivyea_agent.providers import codex_provider
    monkeypatch.setattr(codex_provider.httpx, "post", lambda *a, **k: _Resp())
    result = codex_provider.probe_codex("codex-token", model="gpt-5.3-codex",
                                        base_url="https://chatgpt.com/backend-api/codex")
    assert result["ok"] is True
    assert result["model"] == "gpt-5.3-codex"
    assert result["content"] == "hello"
