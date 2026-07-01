from __future__ import annotations


def test_redact_text_and_object():
    from ivyea_agent import security

    text = "api_key=sk-test1234567890abcdef token: abc123 password='pw'"
    out = security.redact_text(text)
    assert "sk-test" not in out
    assert "abc123" not in out
    assert "pw" not in out
    assert "***REDACTED***" in out

    obj = {"headers": {"Authorization": "Bearer abc"}, "nested": [{"secret": "x"}], "safe": "ok"}
    red = security.redact_obj(obj)
    assert red["headers"]["Authorization"] == "***REDACTED***"
    assert red["nested"][0]["secret"] == "***REDACTED***"
    assert red["safe"] == "ok"


def test_ui_and_trace_redact(ivyea_home):
    from ivyea_agent import traces, ui

    call = ui.tool_call("web_fetch", {"api_key": "sk-test1234567890abcdef"}, color=False)
    assert "sk-test" not in call                       # 友好动词只显示 url，密钥不泄漏
    # 兜底路径（未列入动词表的工具会 dump 参数）必须脱敏
    generic = ui.tool_call("propose_actions", {"api_key": "sk-test1234567890abcdef"}, color=False)
    assert "sk-test" not in generic and "***REDACTED***" in generic

    result = ui.tool_result("token=abc123", color=False)
    assert "abc123" not in result

    traces.record("s", "t", "tool_call", "x", summary="api_key=abc123", payload={"token": "abc"})
    row = traces.recent(limit=1)[0]
    assert "abc123" not in row["summary"]
    assert "abc" not in row["payload"]
