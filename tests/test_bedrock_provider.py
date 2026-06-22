"""AWS Bedrock Converse provider translation tests."""
from __future__ import annotations

import sys
import types

import pytest

from ivyea_agent.providers import bedrock_provider as bp
from ivyea_agent.providers.base import LLMError


def test_bedrock_messages_and_tools_shape():
    system, messages = bp.messages_to_bedrock([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "recall", "arguments": '{"query":"x"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ])
    assert system == [{"text": "sys"}]
    assert messages[0] == {"role": "user", "content": [{"text": "hi"}]}
    assert messages[1]["content"][0]["toolUse"]["name"] == "recall"
    assert messages[2]["content"][0]["toolResult"]["toolUseId"] == "c1"
    tools = bp.tools_to_bedrock([{"type": "function", "function": {"name": "recall", "parameters": {"type": "object"}}}])
    assert tools[0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"


def test_bedrock_extract_response_text_tool_usage():
    out = bp.extract_response({
        "output": {"message": {"content": [
            {"text": "ok"},
            {"toolUse": {"toolUseId": "t1", "name": "recall", "input": {"q": "x"}}},
        ]}},
        "usage": {"inputTokens": 2, "outputTokens": 3},
    })
    assert out["content"] == "ok"
    assert out["tool_calls"] == [{"id": "t1", "name": "recall", "arguments": {"q": "x"}}]
    assert out["usage"] == {"prompt_tokens": 2, "completion_tokens": 3}


def test_from_settings_builds_bedrock_provider(ivyea_home):
    from ivyea_agent.providers import from_settings
    p = from_settings({"kind": "native", "api_mode": "bedrock_converse",
                       "model": "us.amazon.nova-pro-v1:0"}, "")
    assert p.name == "bedrock" and p.model == "us.amazon.nova-pro-v1:0"


def test_bedrock_missing_boto3_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(LLMError) as exc:
        bp._require_boto3()
    assert "boto3" in str(exc.value)
    monkeypatch.delitem(sys.modules, "boto3", raising=False)


def test_bedrock_chat_calls_converse(monkeypatch):
    seen = {}

    class _Client:
        def converse(self, **kwargs):
            seen.update(kwargs)
            return {"output": {"message": {"content": [{"text": "done"}]}},
                    "usage": {"inputTokens": 1, "outputTokens": 1}}

    fake_boto3 = types.SimpleNamespace(client=lambda service, region_name=None: _Client())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    p = bp.BedrockProvider("", "model-x")
    out = p.chat([{"role": "user", "content": "hi"}],
                 tools=[{"type": "function", "function": {"name": "recall", "parameters": {"type": "object"}}}])
    assert out["content"] == "done"
    assert seen["modelId"] == "model-x"
    assert seen["toolConfig"]["tools"][0]["toolSpec"]["name"] == "recall"
    monkeypatch.delitem(sys.modules, "boto3", raising=False)
