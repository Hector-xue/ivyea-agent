"""Claude extended thinking：思考旋钮映射 + thinking 块跨工具轮保留（否则 API 400）。

同时覆盖 API key 路径与 OAuth 路径（都走 AnthropicProvider）。
"""
from __future__ import annotations

from ivyea_agent.providers import anthropic_provider as ap
from ivyea_agent.providers.anthropic_provider import AnthropicProvider


class _Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Msg:
    def __init__(self, content):
        self.content = content
        self.usage = None


class _FakeMessages:
    def __init__(self, outbox, capture):
        self._outbox = outbox
        self._capture = capture

    def create(self, **kw):
        self._capture.append(kw)
        return self._outbox.pop(0)


class _FakeClient:
    def __init__(self, outbox, capture):
        self._m = _FakeMessages(outbox, capture)

    def with_options(self, **kw):
        return self

    @property
    def messages(self):
        return self._m


def _provider(effort, outbox, capture, oauth=False):
    p = AnthropicProvider("k", "claude-sonnet-4-6", oauth=oauth)
    p.reasoning_effort = effort
    p._client = _FakeClient(outbox, capture)   # 绕过真实 SDK
    return p


# ── 思考旋钮映射 ──
def test_thinking_kw_mapping():
    assert ap._thinking_kw("high", 8192) == {"thinking": {"type": "enabled", "budget_tokens": 16384}, "max_tokens": 24576}
    assert ap._thinking_kw("auto", 8192) == {}
    assert ap._thinking_kw("off", 8192) == {}


def test_extract_captures_thinking():
    msg = _Msg([_Blk(type="thinking", thinking="想", signature="S"),
                _Blk(type="tool_use", id="t1", name="grep", input={})])
    ex = ap._extract(msg)
    assert ex["_thinking"] == [{"type": "thinking", "thinking": "想", "signature": "S"}]
    assert ex["tool_calls"][0]["id"] == "t1"


def test_split_reinjects_thinking_first():
    cache = {"t1": [{"type": "thinking", "thinking": "想", "signature": "S"}]}
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "grep", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "r"}]
    _, out = ap._split_messages(msgs, cache)
    assert [b["type"] for b in out[0]["content"]] == ["thinking", "tool_use"]
    # 无 cache 不注入
    _, out2 = ap._split_messages(msgs, None)
    assert [b["type"] for b in out2[0]["content"]] == ["tool_use"]


# ── 集成：缓存并在下一轮请求里回放 thinking 块 ──
def test_thinking_cached_and_replayed_across_tool_loop():
    capture: list = []
    outbox = [
        _Msg([_Blk(type="thinking", thinking="先想", signature="SIG1"),
              _Blk(type="tool_use", id="t1", name="grep", input={"q": "x"})]),
        _Msg([_Blk(type="text", text="最终答案")]),
    ]
    p = _provider("high", outbox, capture)

    # 第 1 轮：模型返回 thinking + tool_use
    ex1 = p.chat([{"role": "user", "content": "查一下"}])
    assert ex1["tool_calls"][0]["id"] == "t1"
    assert capture[0].get("thinking") == {"type": "enabled", "budget_tokens": 16384}   # 请求带思考
    assert capture[0]["max_tokens"] > 16384

    # 第 2 轮：把 assistant(tool_calls) + tool_result 回灌（模拟 agent_loop）
    history = [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "结果"},
    ]
    p.chat(history)
    sent = capture[1]["messages"]           # 第 2 次请求发出的 messages
    asst = next(m for m in sent if m["role"] == "assistant")
    types = [b["type"] for b in asst["content"]]
    assert types[0] == "thinking" and "tool_use" in types      # thinking 块被回放且在最前
    assert asst["content"][0]["signature"] == "SIG1"            # 原样带 signature


def test_no_thinking_param_when_auto():
    capture: list = []
    p = _provider("auto", [_Msg([_Blk(type="text", text="hi")])], capture)
    p.chat([{"role": "user", "content": "hi"}])
    assert "thinking" not in capture[0]


def test_oauth_path_also_gets_thinking():
    """OAuth 路径同走 AnthropicProvider —— 思考同样生效，且 system 仍带 Claude Code 身份。"""
    capture: list = []
    p = _provider("high", [_Msg([_Blk(type="text", text="ok")])], capture, oauth=True)
    p.chat([{"role": "user", "content": "hi"}])
    assert capture[0].get("thinking", {}).get("budget_tokens") == 16384
    assert capture[0]["system"][0]["text"].startswith("You are Claude Code")


def test_stream_yields_reasoning_events():
    """流式：thinking_delta → reasoning 事件，text_delta → text 事件。"""
    class _Ev:
        def __init__(self, type, delta=None):
            self.type = type; self.delta = delta

    class _Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self):
            yield _Ev("content_block_delta", _Blk(type="thinking_delta", thinking="想想"))
            yield _Ev("content_block_delta", _Blk(type="text_delta", text="答"))
        def get_final_message(self):
            return _Msg([_Blk(type="text", text="答")])

    class _M:
        def stream(self, **kw): return _Stream()

    class _C:
        def with_options(self, **kw): return self
        @property
        def messages(self): return _M()

    p = AnthropicProvider("k", "claude-sonnet-4-6")
    p.reasoning_effort = "high"
    p._client = _C()
    evs = list(p.stream_chat([{"role": "user", "content": "hi"}]))
    kinds = [e["type"] for e in evs]
    assert "reasoning" in kinds and "text" in kinds and kinds[-1] == "final"
    assert next(e["text"] for e in evs if e["type"] == "reasoning") == "想想"
