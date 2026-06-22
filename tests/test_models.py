"""Model provider catalog and picker helpers."""
from __future__ import annotations


def test_hermes_style_provider_catalog_contains_key_auth_shapes():
    from ivyea_agent import models
    providers = {p["id"]: p for p in models.providers()}
    for pid in ("openai", "anthropic", "deepseek", "openrouter", "ollama", "google-gemini-cli", "openai-codex", "copilot"):
        assert pid in providers
    assert providers["openai-codex"]["auth_type"] == "oauth_external"
    assert providers["google-gemini-cli"]["auth_type"] == "oauth_external"
    assert providers["copilot"]["auth_type"] == "copilot"
    assert providers["ollama"]["auth_type"] == "none"


def test_model_aliases_and_provider_model_ids_resolve():
    from ivyea_agent import models
    assert models.by_id("deepseek-chat")["provider_id"] == "deepseek"
    assert models.by_id("claude-sonnet")["kind"] == "anthropic"
    custom = models.by_id("openrouter:moonshotai/kimi-k2.6")
    assert custom["provider_id"] == "openrouter"
    assert custom["model"] == "moonshotai/kimi-k2.6"


def test_key_status_for_api_key_oauth_and_none(monkeypatch):
    from ivyea_agent import models
    deepseek = models.provider_by_id("deepseek")
    ollama = models.provider_by_id("ollama")
    codex = models.provider_by_id("openai-codex")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert models.key_status(deepseek).startswith("missing:")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk")
    assert models.key_status(deepseek) == "configured"
    assert models.key_status(ollama) == "none"
    assert models.key_status(codex) == "missing:auth-token"


def test_cli_model_provider_and_doctor_outputs(ivyea_home, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, config, models
    config.apply_model(models.by_id("ollama:qwen3-coder"))
    rc = cli._cmd_model(Namespace(spec="providers", extra=None, token=None,
                                  refresh_token=None, expires_at=0))
    out = capsys.readouterr().out
    assert rc == 0 and "openai-codex" in out and "ollama" in out
    rc = cli._cmd_model(Namespace(spec="doctor", extra=None, token=None,
                                  refresh_token=None, expires_at=0))
    out = capsys.readouterr().out
    assert rc == 0 and "OK 当前模型配置可进入对话" in out


def test_cli_model_direct_oauth_requires_auth(ivyea_home, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, config
    rc = cli._cmd_model(Namespace(spec="openai-codex:gpt-5-codex", extra=None, token=None,
                                  refresh_token=None, expires_at=0))
    out = capsys.readouterr().out
    assert rc == 1
    assert "暂不切换主脑" in out
    assert "ivyea model auth openai-codex --device-code" in out
    assert config.load_settings().get("provider_id") != "openai-codex"


def test_model_picker_lists_providers_first(ivyea_home, monkeypatch, capsys):
    from ivyea_agent import cli
    answers = iter(["", ""])
    monkeypatch.setattr(cli, "_ask", lambda prompt, default="": next(answers, default))
    cli._model_picker()
    out = capsys.readouterr().out
    assert "OpenAI API" in out
    assert "OpenAI API · gpt-4o" not in out
    assert "gpt-5-codex" not in out


def test_cli_model_auth_imports_token(ivyea_home, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, config, models, oauth_auth
    rc = cli._cmd_model(Namespace(spec="auth", extra="qwen-oauth", token="secret-token",
                                  refresh_token="", expires_at=0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "secret-token" not in out
    assert oauth_auth.get_token("qwen-oauth") == "secret-token"
    config.apply_model(models.by_id("qwen-oauth:qwen3-coder-plus"))
    assert config.get_active_key() == "secret-token"


def test_cli_model_auth_imports_qwen_cli(ivyea_home, tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    qwen_dir = tmp_path / ".qwen"
    qwen_dir.mkdir()
    (qwen_dir / "oauth_creds.json").write_text(
        '{"access_token":"qwen-cli-token","refresh_token":"ref","expiry_date":1893456000000}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = cli._cmd_model(Namespace(spec="auth", extra="qwen-oauth", token=None,
                                  refresh_token=None, expires_at=0,
                                  import_qwen_cli=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "qwen-cli-token" not in out
    assert oauth_auth.get_token("qwen-oauth") == "qwen-cli-token"


def test_cli_model_auth_refreshes_qwen(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    oauth_auth.set_auth_token("qwen-oauth", "old", refresh_token="ref")
    monkeypatch.setattr(oauth_auth, "refresh_qwen_token", lambda: "new")
    rc = cli._cmd_model(Namespace(spec="auth", extra="qwen-oauth", token=None,
                                  refresh_token=None, expires_at=0,
                                  refresh=True, import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "new" not in out
    assert "已刷新" in out


def test_cli_model_auth_device_code_codex(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    monkeypatch.setattr(oauth_auth, "codex_device_code_login", lambda notify=None: None)
    rc = cli._cmd_model(Namespace(spec="auth", extra="openai-codex", token=None,
                                  refresh_token=None, expires_at=0,
                                  refresh=False, device_code=True,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Codex Responses transport 已接入" in out


def test_cli_model_auth_exchange_copilot(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    monkeypatch.setattr(oauth_auth, "resolve_copilot_api_token", lambda strict=False: "copilot-api")
    rc = cli._cmd_model(Namespace(spec="auth", extra="copilot", token=None,
                                  refresh_token=None, expires_at=0,
                                  refresh=False, device_code=False,
                                  exchange=True, import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "copilot-api" not in out
    assert "Copilot API token" in out


def test_cli_model_auth_login_google_gemini(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    monkeypatch.setattr(oauth_auth, "google_oauth_login", lambda open_browser=True, notify=None: None)
    rc = cli._cmd_model(Namespace(spec="auth", extra="google-gemini-cli", token=None,
                                  refresh_token=None, expires_at=0,
                                  refresh=False, login=True, no_browser=True,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "已完成 OAuth 登录" in out


def test_cli_model_auth_google_gemini_project(ivyea_home, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    rc = cli._cmd_model(Namespace(spec="auth", extra="google-gemini-cli", token=None,
                                  refresh_token=None, expires_at=0, project="project-1",
                                  refresh=False, login=False, no_browser=False,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "project-1" in out
    assert oauth_auth.google_project_id() == "project-1"


def test_cli_model_auth_detail_hides_token(ivyea_home, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    oauth_auth.set_auth_token("google-gemini-cli", "secret-token", refresh_token="refresh-secret", expires_at=1893456000)
    oauth_auth.set_google_project_id("project-1")
    rc = cli._cmd_model(Namespace(spec="auth", extra="google-gemini-cli", token=None,
                                  refresh_token=None, expires_at=0, project=None,
                                  refresh=False, login=False, no_browser=False,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "project-1" in out
    assert "present" in out
    assert "secret-token" not in out
    assert "refresh-secret" not in out


def test_cli_model_auth_google_gemini_probe(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    oauth_auth.set_auth_token("google-gemini-cli", "secret-token")

    def fake_probe(token, model="gemini-3-pro-preview", timeout=30.0):
        assert token == "secret-token"
        return {"ok": True, "model": model, "project": "project-1", "content": "OK", "usage": {"total": 1}}

    from ivyea_agent.providers import gemini_code_assist_provider as mod
    monkeypatch.setattr(mod, "probe_gemini_code_assist", fake_probe)
    rc = cli._cmd_model(Namespace(spec="auth", extra="google-gemini-cli", token=None,
                                  refresh_token=None, expires_at=0, project=None,
                                  probe=True, timeout=5.0,
                                  refresh=False, login=False, no_browser=False,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "probe 成功" in out
    assert "project-1" in out
    assert "secret-token" not in out


def test_cli_model_auth_google_gemini_probe_missing_token(ivyea_home, capsys):
    from argparse import Namespace
    from ivyea_agent import cli
    rc = cli._cmd_model(Namespace(spec="auth", extra="google-gemini-cli", token=None,
                                  refresh_token=None, expires_at=0, project=None,
                                  probe=True, timeout=5.0,
                                  refresh=False, login=False, no_browser=False,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    err = capsys.readouterr().err
    assert rc == 1
    assert "token 未配置" in err


def test_cli_model_auth_google_gemini_probe_diagnoses_failure(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    oauth_auth.set_auth_token("google-gemini-cli", "secret-token")

    def fake_probe(token, model="gemini-3-pro-preview", timeout=30.0):
        from ivyea_agent.providers.gemini_code_assist_provider import GeminiCodeAssistError
        raise GeminiCodeAssistError("Gemini Code Assist HTTP 404", status_code=404, body="project not found")

    from ivyea_agent.providers import gemini_code_assist_provider as mod
    monkeypatch.setattr(mod, "probe_gemini_code_assist", fake_probe)
    rc = cli._cmd_model(Namespace(spec="auth", extra="google-gemini-cli", token=None,
                                  refresh_token=None, expires_at=0, project=None,
                                  probe=True, timeout=5.0,
                                  refresh=False, login=False, no_browser=False,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    err = capsys.readouterr().err
    assert rc == 1
    assert "project" in err
    assert "secret-token" not in err


def test_cli_model_auth_codex_probe(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    oauth_auth.set_auth_token("openai-codex", "secret-token")

    def fake_probe(token, model="gpt-5.3-codex", base_url="", timeout=30.0):
        assert token == "secret-token"
        return {"ok": True, "model": model, "content": "OK", "usage": {"total": 1}}

    from ivyea_agent.providers import codex_provider
    monkeypatch.setattr(codex_provider, "probe_codex", fake_probe)
    rc = cli._cmd_model(Namespace(spec="auth", extra="openai-codex", token=None,
                                  refresh_token=None, expires_at=0, project=None,
                                  probe=True, timeout=5.0,
                                  refresh=False, login=False, no_browser=False,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "openai-codex probe 成功" in out
    assert "secret-token" not in out


def test_cli_model_auth_copilot_probe(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    oauth_auth.set_auth_token("copilot", "secret-token")

    def fake_probe(token, model="gpt-4o", base_url="", timeout=30.0):
        assert token == "secret-token"
        return {"ok": True, "model": model, "content": "OK", "usage": {"total": 1}}

    from ivyea_agent.providers import copilot_provider
    monkeypatch.setattr(copilot_provider, "probe_copilot", fake_probe)
    rc = cli._cmd_model(Namespace(spec="auth", extra="copilot", token=None,
                                  refresh_token=None, expires_at=0, project=None,
                                  probe=True, timeout=5.0,
                                  refresh=False, login=False, no_browser=False,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "copilot probe 成功" in out
    assert "secret-token" not in out


def test_cli_model_auth_qwen_probe(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    oauth_auth.set_auth_token("qwen-oauth", "secret-token")

    def fake_probe(token, model="qwen3.7-max", base_url="", timeout=30.0):
        assert token == "secret-token"
        assert "qwen" in model
        assert "portal.qwen.ai" in base_url
        return {"ok": True, "model": model, "content": "OK", "usage": {"total_tokens": 1}}

    from ivyea_agent.providers import openai_compat
    monkeypatch.setattr(openai_compat, "probe_openai_compat", fake_probe)
    rc = cli._cmd_model(Namespace(spec="auth", extra="qwen-oauth", token=None,
                                  refresh_token=None, expires_at=0, project=None,
                                  probe=True, timeout=5.0,
                                  refresh=False, login=False, no_browser=False,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "qwen-oauth probe 成功" in out
    assert "secret-token" not in out


def test_cli_model_auth_login_qwen(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    monkeypatch.setattr(oauth_auth, "qwen_cli_login", lambda: None)
    rc = cli._cmd_model(Namespace(spec="auth", extra="qwen-oauth", token=None,
                                  refresh_token=None, expires_at=0,
                                  refresh=False, login=True, no_browser=False,
                                  device_code=False, exchange=False,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "已完成 OAuth 登录" in out


def test_cli_model_auth_device_code_qwen(ivyea_home, monkeypatch, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    monkeypatch.setattr(oauth_auth, "qwen_device_code_login", lambda open_browser=True, notify=None: None)
    rc = cli._cmd_model(Namespace(spec="auth", extra="qwen-oauth", token=None,
                                  refresh_token=None, expires_at=0, project=None,
                                  probe=False, timeout=30.0,
                                  refresh=False, login=False, no_browser=True,
                                  device_code=True, exchange=False,
                                  import_qwen_cli=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-04-15" in out
    assert "已保存本地认证" in out


def test_cli_model_logout_clears_token(ivyea_home, capsys):
    from argparse import Namespace
    from ivyea_agent import cli, oauth_auth
    oauth_auth.set_auth_token("qwen-oauth", "secret-token")
    rc = cli._cmd_model(Namespace(spec="logout", extra="qwen-oauth", token=None,
                                  refresh_token=None, expires_at=0,
                                  import_qwen_cli=False))
    assert rc == 0
    assert oauth_auth.get_token("qwen-oauth") == ""
