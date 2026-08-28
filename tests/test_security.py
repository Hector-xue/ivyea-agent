from __future__ import annotations

from ivyea_agent import security


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


# ── 占位符不是密钥（实测打出来的） ───────────────────────────────────────────
#
# read_file 读一个 shell 脚本时，`-H "X-Token: $REPORT_TOKEN"` 被抹成
# `X-Token=***REDACTED***`，模型据此得出"这个脚本硬编码了 token"的错误结论，
# 并把这句话写进了它生成的技能文档。
#
# 危害不止于看错：脱敏加在 `_truncate` 上，**模型读到的每一份源码**都可能被悄悄改写；
# 模型照抄读到的那一行去 edit_file，old 永远匹配不上文件真实内容。
def test_variable_references_are_not_secrets():
    for text in ('curl -H "X-Token: $REPORT_TOKEN"',
                 'export API_KEY=${MY_KEY}',
                 'password=%DB_PASS%',
                 'token = os.environ["REPORT_TOKEN"]',
                 'api_key = os.getenv("KEY")',
                 'const token = process.env.TOKEN',
                 'api_key: <your-key-here>',
                 'secret: {{vault_secret}}',
                 'token: xxxxxxxx',
                 'password: ****'):
        assert "REDACTED" not in security.redact_text(text), text


def test_real_secrets_are_still_redacted():
    for text in ('api_key=sk-test1234567890abcdef',
                 'password=hunter2correct',
                 'secret: 9f8e7d6c5b4a3210',
                 'token="ghp_ABCdefGHIjklMNOpqrSTUvwxYZ0123"'):
        assert "***REDACTED***" in security.redact_text(text), text


def test_an_auth_scheme_prefix_does_not_shield_the_token():
    """不吃掉 Bearer，就只会抹掉 "Bearer" 这个词，真正的 JWT 原样留在日志里。"""
    out = security.redact_text("authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    out = security.redact_text("Authorization: Basic dXNlcjpwYXNzd29yZA==")
    assert "dXNlcjpwYXNz" not in out


def test_redaction_is_idempotent():
    once = security.redact_text("api_key=sk-test1234567890abcdef")
    assert security.redact_text(once) == once


def test_source_code_survives_a_round_trip_through_read_file(tmp_path):
    """模型读到的源码必须**和磁盘上一字不差** —— 否则它照抄去 edit_file 会匹配失败。"""
    from ivyea_agent import tools_general
    from ivyea_agent.agent_tools import ToolContext

    script = tmp_path / "run.sh"
    script.write_text('curl -H "X-Token: $REPORT_TOKEN" https://api.example.com\n', encoding="utf-8")
    out = tools_general.t_read_file({"path": "run.sh"}, ToolContext(workspace=str(tmp_path)))
    assert "$REPORT_TOKEN" in out
    assert "REDACTED" not in out
