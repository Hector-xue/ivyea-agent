from __future__ import annotations


def test_notify_stdout_redacts_secret(ivyea_home):
    from ivyea_agent import notify

    result = notify.send("token=secret-value", title="Alert", channel="stdout")
    assert result["ok"] is True
    assert "secret-value" not in result["message"]
    assert "***REDACTED***" in result["message"]


def test_notify_webhook_payload(monkeypatch, ivyea_home):
    from ivyea_agent import notify

    seen = {}

    class _Resp:
        status_code = 204

        def raise_for_status(self):
            return None

    def fake_post(url, json, timeout):
        seen["url"] = url
        seen["json"] = json
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    result = notify.send("hello", title="Ivyea", channel="webhook", webhook_url="https://example.test/hook")
    assert result == {"ok": True, "channel": "webhook", "status_code": 204}
    assert seen["url"] == "https://example.test/hook"
    assert seen["json"]["title"] == "Ivyea"
    assert seen["json"]["text"] == "hello"


def test_notify_feishu_payload():
    from ivyea_agent import notify

    payload = notify.build_payload("Ivyea", "hello", channel="feishu")
    assert payload["msg_type"] == "text"
    assert payload["content"]["text"].startswith("Ivyea")


def test_notify_cli_stdout(ivyea_home, capsys):
    from ivyea_agent.cli import main

    assert main(["notify", "test", "--message", "hello"]) == 0
    out = capsys.readouterr().out
    assert "Ivyea Agent" in out
    assert "hello" in out
