"""Phase B：主脑健康探测、debug 日志、engineering_context 缓存。"""
from __future__ import annotations

import importlib


# ── B1 主脑健康（本地判断，不发网络）──
def test_main_brain_health_oauth_expired(ivyea_home, monkeypatch):
    from ivyea_agent import config, oauth_auth
    monkeypatch.setattr(config, "load_settings",
                        lambda: {"auth_type": "oauth_external", "provider_id": "openai-codex", "label": "Codex"})
    monkeypatch.setattr(oauth_auth, "token_status", lambda pid: "expired")
    h = config.main_brain_health()
    assert h["ok"] is False and h["status"] == "expired" and "Codex" in h["hint"]


def test_main_brain_health_api_key_ok(ivyea_home, monkeypatch):
    from ivyea_agent import config
    monkeypatch.setattr(config, "load_settings", lambda: {"auth_type": "api_key", "label": "DeepSeek"})
    monkeypatch.setattr(config, "get_active_key", lambda: "sk-xxx")
    assert config.main_brain_health()["ok"] is True


# ── B2 debug 日志开关 ──
def test_log_dbg_writes_only_when_enabled(ivyea_home, monkeypatch):
    from ivyea_agent import config, log
    importlib.reload(log)
    logfile = config.IVYEA_DIR / "logs" / "agent.log"
    # 关闭：不写
    monkeypatch.delenv("IVYEA_DEBUG", raising=False)
    monkeypatch.setattr(log, "enabled", lambda: False)
    log.dbg("test", "should-not-write")
    assert not logfile.exists()
    # 开启：落盘
    monkeypatch.setattr(log, "enabled", lambda: True)
    log.dbg("test", "hello-debug")
    assert logfile.exists() and "hello-debug" in logfile.read_text(encoding="utf-8")


# ── B3 engineering_context 缓存 ──
def test_engineering_context_caches_inspect(monkeypatch, tmp_path):
    from ivyea_agent import engineering_context as ec
    ec._STATIC_CACHE.clear()
    calls = {"n": 0}

    def fake_inspect(root):
        calls["n"] += 1
        return {"entrypoints": [], "tests": ["t_x.py"], "configs": [], "suggested_commands": []}

    monkeypatch.setattr(ec.workspace, "project_inspect", fake_inspect)
    monkeypatch.setattr(ec.skills, "status", lambda: [])
    monkeypatch.setattr(ec.git_workflow, "status",
                        lambda root: {"ok": True, "branch": "main", "head": "abc123", "clean": True, "changes": []})
    q = "重构这个 python 模块"   # should_include=True
    a = ec.build(str(tmp_path), q)
    b = ec.build(str(tmp_path), q)
    assert "tests: t_x.py" in a and a == b
    assert calls["n"] == 1   # 第二次走缓存，project_inspect 只调一次


def test_engineering_context_cache_invalidates_on_head_change(monkeypatch, tmp_path):
    from ivyea_agent import engineering_context as ec
    ec._STATIC_CACHE.clear()
    calls = {"n": 0}
    monkeypatch.setattr(ec.workspace, "project_inspect",
                        lambda root: (calls.__setitem__("n", calls["n"] + 1) or {"entrypoints": [], "tests": [], "configs": [], "suggested_commands": []}))
    monkeypatch.setattr(ec.skills, "status", lambda: [])
    heads = iter(["h1", "h2"])
    monkeypatch.setattr(ec.git_workflow, "status",
                        lambda root: {"ok": True, "branch": "main", "head": next(heads), "clean": True, "changes": []})
    ec.build(str(tmp_path), "修复 bug")
    ec.build(str(tmp_path), "修复 bug")
    assert calls["n"] == 2   # head 变 → 缓存失效，重算
