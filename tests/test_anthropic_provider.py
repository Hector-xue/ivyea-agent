"""Anthropic provider：OpenAI↔Anthropic 格式归一、caching 标记、usage 归一、pricing。

纯翻译/计算逻辑，离线（不调真实 Claude API）。
"""
from __future__ import annotations

from ivyea_agent.providers import anthropic_provider as ap


def test_split_system_and_user():
    system, msgs = ap._split_messages([
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "看下广告"},
    ])
    assert system == "你是助手"
    assert msgs == [{"role": "user", "content": [{"type": "text", "text": "看下广告"}]}]


def test_assistant_tool_calls_to_tool_use():
    _, msgs = ap._split_messages([
        {"role": "user", "content": "巡检"},
        {"role": "assistant", "content": "好的", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "run_patrol", "arguments": '{"sid": 1876}'}}]},
    ])
    asst = msgs[-1]
    assert asst["role"] == "assistant"
    assert asst["content"][0] == {"type": "text", "text": "好的"}
    tu = asst["content"][1]
    assert tu == {"type": "tool_use", "id": "c1", "name": "run_patrol", "input": {"sid": 1876}}


def test_tool_results_merged_into_one_user_msg():
    _, msgs = ap._split_messages([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果1"},
        {"role": "tool", "tool_call_id": "c2", "content": "结果2"},
    ])
    # 两个 tool 结果应合并进同一条 user 消息（Anthropic 要求 tool_result 在 user 里）
    user = msgs[-1]
    assert user["role"] == "user"
    assert [b["type"] for b in user["content"]] == ["tool_result", "tool_result"]
    assert user["content"][0]["tool_use_id"] == "c1"
    assert user["content"][1]["content"] == "结果2"


def test_tools_to_anthropic_schema():
    out = ap._tools_to_anthropic([
        {"type": "function", "function": {
            "name": "run_patrol", "description": "巡检",
            "parameters": {"type": "object", "properties": {"sid": {"type": "integer"}}}}}])
    assert out == [{"name": "run_patrol", "description": "巡检",
                    "input_schema": {"type": "object", "properties": {"sid": {"type": "integer"}}}}]


def test_tools_none():
    assert ap._tools_to_anthropic(None) is None
    assert ap._tools_to_anthropic([]) is None


def test_system_param_has_cache_control():
    blocks = ap._system_param("frozen system", cache=True)
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[0]["text"] == "frozen system"
    assert ap._system_param("") is None


class _Usage:
    input_tokens = 100
    output_tokens = 20
    cache_read_input_tokens = 80
    cache_creation_input_tokens = 0


def test_norm_usage():
    u = ap._norm_usage(_Usage())
    assert u == {"prompt_tokens": 180, "completion_tokens": 20, "prompt_cache_hit_tokens": 80}


class _Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Msg:
    def __init__(self, content, usage):
        self.content = content
        self.usage = usage


def test_extract_text_and_tool_use():
    msg = _Msg([
        _Blk(type="text", text="结论："),
        _Blk(type="tool_use", id="c1", name="propose_actions", input={"x": 1}),
    ], _Usage())
    ex = ap._extract(msg)
    assert ex["content"] == "结论："
    assert ex["tool_calls"] == [{"id": "c1", "name": "propose_actions", "arguments": {"x": 1}}]
    assert ex["usage"]["prompt_cache_hit_tokens"] == 80


def test_pricing_for_claude(ivyea_home):
    from ivyea_agent import pricing
    # opus 4.8: input ¥36/M, output ¥180/M
    c = pricing.estimate("claude-opus-4-8", {"prompt_tokens": 1_000_000, "completion_tokens": 0})
    assert abs(c - 36.0) < 1e-6
    # 全缓存命中 → cached_input ¥3.6/M
    c2 = pricing.estimate("claude-opus-4-8",
                          {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000,
                           "completion_tokens": 0})
    assert abs(c2 - 3.6) < 1e-6


def test_from_settings_builds_anthropic(ivyea_home):
    from ivyea_agent.providers import from_settings
    p = from_settings({"kind": "anthropic", "model": "claude-opus-4-8",
                       "base_url": "https://api.deepseek.com"}, "sk-test")
    # 切换残留的 deepseek base_url 必须被忽略（不会发到 Anthropic SDK）
    assert p.name == "anthropic" and p.model == "claude-opus-4-8" and p.base_url == ""
