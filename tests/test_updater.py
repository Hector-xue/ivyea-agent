"""版本更新检测：版本比较、缓存读取、更新命令。"""
from __future__ import annotations

import json
import time


def test_norm_and_has_update():
    from ivyea_agent import updater
    assert updater._norm("v1.10.0") > updater._norm("v1.9.0")   # 1.10 > 1.9（非字典序）
    assert updater._norm("v2.0.0") > updater._norm("1.9.9")
    assert updater.has_update("1.1.2", "v1.1.3") is True
    assert updater.has_update("1.1.2", "v1.1.2") is False
    assert updater.has_update("1.1.3", "v1.1.2") is False        # 本地更新则不提示
    assert updater.has_update("1.1.2", None) is False            # 离线/拉取失败


def test_check_latest_uses_fresh_cache(ivyea_home, monkeypatch):
    from ivyea_agent import updater, config, __version__
    config.ensure_dirs()
    (config.IVYEA_DIR / "update_check.json").write_text(
        json.dumps({"latest": "v999.0.0", "checked_at": time.time()}), encoding="utf-8")
    # 缓存新鲜 → 不应发网络
    monkeypatch.setattr(updater, "_fetch_latest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发网络")))
    r = updater.check_latest()
    assert r["latest"] == "v999.0.0" and r["has_update"] is True and r["current"] == __version__


def test_check_now_writes_cache(ivyea_home, monkeypatch):
    from ivyea_agent import updater, config
    monkeypatch.setattr(updater, "_fetch_latest", lambda *a, **k: "v9.9.9")
    r = updater.check_now()
    assert r["latest"] == "v9.9.9" and r["has_update"] is True
    cached = json.loads((config.IVYEA_DIR / "update_check.json").read_text(encoding="utf-8"))
    assert cached["latest"] == "v9.9.9"


def test_update_commands_source_repo():
    from ivyea_agent import updater
    cmds = updater.update_commands()          # 本仓即源码仓 → git pull
    assert cmds and cmds[0][0] == "git" and "pull" in cmds[0]
