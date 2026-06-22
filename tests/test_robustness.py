"""Tier 1 健壮性：限流重试/退避 + 降级链。"""
from __future__ import annotations

import pytest

from ivyea_agent.providers import openai_compat as oc
from ivyea_agent.providers.base import LLMError
from ivyea_agent.providers.chain import ChainProvider


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}
        self.text = "err"
    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(oc.time, "sleep", lambda *_: None)


def test_post_retries_then_succeeds(monkeypatch):
    seq = [503, 200]
    def fake_post(*a, **k):
        return _Resp(seq.pop(0))
    monkeypatch.setattr(oc.httpx, "post", fake_post)
    p = oc.OpenAICompatProvider("k", "deepseek-chat", "https://x")
    out = p.chat([{"role": "user", "content": "hi"}])
    assert out["content"] == "ok" and not seq   # 两次都用上(503 重试→200)


def test_post_non_retryable_raises_immediately(monkeypatch):
    calls = {"n": 0}
    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp(401)
    monkeypatch.setattr(oc.httpx, "post", fake_post)
    p = oc.OpenAICompatProvider("k", "m", "https://x")
    with pytest.raises(LLMError):
        p.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1   # 401 不重试


def test_post_all_retryable_exhausts(monkeypatch):
    calls = {"n": 0}
    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp(429)
    monkeypatch.setattr(oc.httpx, "post", fake_post)
    p = oc.OpenAICompatProvider("k", "m", "https://x")
    with pytest.raises(LLMError):
        p.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == oc._RETRIES   # 用满重试次数


# ── 降级链 ──
class _Fake:
    def __init__(self, model, fail=False, fail_after_yield=False):
        self.model = model; self.api_key = "k"
        self.fail = fail; self.fail_after_yield = fail_after_yield
    def stream_chat(self, messages, tools=None, **kw):
        if self.fail and not self.fail_after_yield:
            raise LLMError(f"{self.model} 挂了")
        yield {"type": "text", "text": f"[{self.model}]"}
        if self.fail_after_yield:
            raise LLMError("吐到一半挂了")
        yield {"type": "final", "content": f"[{self.model}]", "tool_calls": [], "usage": {}}
    def chat(self, messages, tools=None, **kw):
        if self.fail:
            raise LLMError(f"{self.model} 挂了")
        return {"role": "assistant", "content": self.model, "tool_calls": []}


def test_chain_falls_back_before_any_output():
    chain = ChainProvider([(_Fake("主", fail=True), "主"), (_Fake("备"), "备")])
    evs = list(chain.stream_chat([{"role": "user", "content": "x"}]))
    assert evs[-1]["content"] == "[备]"   # 主在吐字前挂 → 切到备


def test_chain_reraises_if_failed_mid_stream():
    chain = ChainProvider([(_Fake("主", fail=True, fail_after_yield=True), "主"), (_Fake("备"), "备")])
    with pytest.raises(LLMError):
        list(chain.stream_chat([{"role": "user", "content": "x"}]))  # 已吐内容→不降级，抛


def test_chain_all_fail():
    chain = ChainProvider([(_Fake("主", fail=True), "主"), (_Fake("备", fail=True), "备")])
    with pytest.raises(LLMError):
        list(chain.stream_chat([{"role": "user", "content": "x"}]))


def test_chain_chat_fallback():
    chain = ChainProvider([(_Fake("主", fail=True), "主"), (_Fake("备"), "备")])
    assert chain.chat([{"role": "user", "content": "x"}])["content"] == "备"


def test_build_chain_with_fallback(ivyea_home):
    from ivyea_agent import config
    from ivyea_agent.providers import build_chain
    config.set_env_key("DEEPSEEK_API_KEY", "sk-x")
    config.set_setting("fallback_models", "deepseek-reasoner")   # 同 DEEPSEEK key → 可用
    prov = build_chain({"kind": "openai", "model": "deepseek-chat",
                        "base_url": "https://api.deepseek.com", "label": "DeepSeek"}, "sk-x")
    assert isinstance(prov, ChainProvider) and len(prov.members) == 2


def test_build_chain_no_fallback_returns_single(ivyea_home):
    from ivyea_agent.providers import build_chain
    prov = build_chain({"kind": "openai", "model": "deepseek-chat",
                        "base_url": "https://api.deepseek.com"}, "sk-x")
    assert not isinstance(prov, ChainProvider)   # 无备用 → 不套链


def test_oauth_provider_reports_transport_not_member_login():
    from ivyea_agent.providers import from_settings
    with pytest.raises(LLMError) as exc:
        from_settings({"kind": "oauth", "auth_type": "oauth_external",
                       "label": "OpenAI Codex OAuth", "model": "gpt-5-codex"}, "")
    msg = str(exc.value)
    assert "oauth_external" in msg
    assert "登录制" not in msg
    assert "普通会员" in msg
