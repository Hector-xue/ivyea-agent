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


class _UnsupportedResp:
    status_code = 400
    text = '{"detail":"The model is not supported when using Codex with a ChatGPT account."}'


class _StreamResp:
    def __init__(self, *, status_code=200, lines=None, body=""):
        self.status_code = status_code
        self._lines = lines or [
            'data: {"type":"response.output_text.delta","delta":"hello"}',
            'data: {"type":"response.output_item.added","output_index":1,'
            '"item":{"type":"function_call","call_id":"call_1","name":"read_file"}}',
            'data: {"type":"response.function_call_arguments.delta","output_index":1,'
            '"delta":"{\\"path\\":\\"README.md\\"}"}',
            'data: {"type":"response.completed","response":{"usage":{"total_tokens":1}}}',
        ]
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return self._body


def test_from_settings_builds_codex_provider(ivyea_home):
    from ivyea_agent.providers import from_settings
    p = from_settings({"kind": "oauth", "api_mode": "codex_responses",
                       "model": "gpt-5.5",
                       "base_url": "https://chatgpt.com/backend-api/codex"}, "tok")
    assert p.name == "openai-codex"
    assert p.model == "gpt-5.5"


def test_codex_provider_payload_and_response(monkeypatch):
    from ivyea_agent.providers.codex_provider import CodexProvider
    captured = {}

    def fake_stream(method, url, headers=None, json=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _StreamResp()

    from ivyea_agent.providers import codex_provider
    monkeypatch.setattr(codex_provider.httpx, "stream", fake_stream)
    p = CodexProvider("codex-token", "gpt-5.5", "https://chatgpt.com/backend-api/codex")
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
    assert captured["method"] == "POST"
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["Authorization"] == "Bearer codex-token"
    assert captured["headers"]["originator"] == "codex_cli_rs"
    assert captured["json"]["instructions"] == "sys"
    assert captured["json"]["store"] is False
    assert captured["json"]["stream"] is True
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
    # usage 归一成内部契约（chat-completions 形状），否则 agent_loop 累计读不到全是 0
    assert events[-1]["usage"]["prompt_tokens"] == 2
    assert events[-1]["usage"]["completion_tokens"] == 3
    assert events[-1]["usage"]["prompt_cache_hit_tokens"] == 0


def test_normalize_usage_shapes():
    from ivyea_agent.providers.codex_provider import _normalize_usage
    # Responses API 形状 → 归一（含缓存命中）
    u = _normalize_usage({"input_tokens": 100, "output_tokens": 20,
                          "input_tokens_details": {"cached_tokens": 60}, "total_tokens": 120})
    assert u == {"prompt_tokens": 100, "completion_tokens": 20,
                 "prompt_cache_hit_tokens": 60, "prompt_tokens_details": {"cached_tokens": 60}}
    # 已是 chat-completions 形状 → 原样返回
    cc = {"prompt_tokens": 5, "completion_tokens": 1}
    assert _normalize_usage(cc) is cc
    # 空/非法 → 空 dict
    assert _normalize_usage({}) == {}
    assert _normalize_usage(None) == {}


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
    p = CodexProvider("codex-token", "gpt-5.5", "https://chatgpt.com/backend-api/codex")
    events = list(p.stream_chat([{"role": "user", "content": "hi"}]))
    assert captured["method"] == "POST"
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["json"]["stream"] is True
    assert captured["json"]["store"] is False
    assert captured["headers"]["Authorization"] == "Bearer codex-token"
    assert events[0] == {"type": "text", "text": "hi"}
    assert events[-1]["content"] == "hi"
    # 只有 total_tokens 的 usage 也走归一（prompt/completion 缺省为 0，不再透传原始形状）
    assert events[-1]["usage"]["prompt_tokens"] == 0
    assert events[-1]["usage"]["completion_tokens"] == 0


def test_probe_codex(monkeypatch):
    from ivyea_agent.providers import codex_provider
    monkeypatch.setattr(codex_provider.httpx, "stream", lambda *a, **k: _StreamResp())
    result = codex_provider.probe_codex("codex-token", model="gpt-5.5",
                                        base_url="https://chatgpt.com/backend-api/codex")
    assert result["ok"] is True
    assert result["model"] == "gpt-5.5"
    assert result["content"] == "hello"


def test_codex_provider_falls_back_from_unsupported_model(monkeypatch):
    from ivyea_agent.providers.codex_provider import CodexProvider
    seen = []

    def fake_stream(method, url, headers=None, json=None, timeout=None):
        seen.append(json["model"])
        if json["model"] == "gpt-5.3-codex":
            return _StreamResp(status_code=400, body=_UnsupportedResp.text)
        return _StreamResp()

    from ivyea_agent.providers import codex_provider
    monkeypatch.setattr(codex_provider.httpx, "stream", fake_stream)
    p = CodexProvider("codex-token", "gpt-5.3-codex", "https://chatgpt.com/backend-api/codex")
    out = p.chat([{"role": "user", "content": "hi"}])
    assert seen[:2] == ["gpt-5.3-codex", "gpt-5.5"]
    assert p.model == "gpt-5.5"
    assert out["content"] == "hello"
