"""版本更新检测：版本比较、缓存读取、更新命令。"""
from __future__ import annotations

import json
import time
import types


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


def test_update_commands_never_fall_back_to_pypi_or_main(monkeypatch):
    """pip/pipx 路径必须装 **release tag 的 git 源**，不许碰 PyPI，也不许装 main。

    两个实测事实：
      1. `ivyea-agent` 从来没发布到 PyPI（`pip index versions` → 404），旧实现却
         对 pip 用户返回 `pip install --upgrade ivyea-agent`，那条命令必然失败；
         而且这个名字空着可被抢注，届时会给用户装上别人的包。
      2. 更新检测比的是 GitHub 最新 release tag，装 main 就意味着"提示更新到
         v1.13.0、实际装了含未发布代码的 main"。
    """
    from ivyea_agent import updater

    monkeypatch.setattr(updater, "_source_repo", lambda: None)   # 非源码仓 → 走 pip
    cmds = updater.update_commands("v9.9.9")
    assert cmds, "有 ref 就该给出命令"
    flat = " ".join(cmds[0])
    assert "git+https://github.com/Hector-xue/ivyea-agent.git@v9.9.9" in flat
    assert "@main" not in flat
    # 末尾那个孤零零的包名（PyPI 目标）不许再出现
    assert cmds[0][-1].startswith("git+"), cmds
    # 新增依赖必须能装上：1.13.0 就加了 Pillow / rapidocr，--no-deps 会让新功能装完即坏
    assert "--no-deps" not in flat


def test_update_commands_refuse_without_a_ref(monkeypatch):
    """解析不出 release tag 时宁可什么都不做，也不能瞎装。"""
    from ivyea_agent import updater

    monkeypatch.setattr(updater, "_source_repo", lambda: None)
    assert updater.update_commands("") == []


def test_do_update_aborts_when_release_cannot_be_resolved(monkeypatch):
    from ivyea_agent import updater

    monkeypatch.setattr(updater, "_source_repo", lambda: None)
    monkeypatch.setattr(updater, "_fetch_latest", lambda timeout=8.0: None)
    ok, out = updater.do_update()
    assert ok is False
    assert "故意不回退" in out


def test_do_update_uses_the_resolved_tag(monkeypatch):
    from ivyea_agent import updater

    ran = []
    monkeypatch.setattr(updater, "_source_repo", lambda: None)
    monkeypatch.setattr(updater, "_fetch_latest", lambda timeout=8.0: "v1.13.1")
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or types.SimpleNamespace(returncode=0, stdout="done"))
    ok, _out = updater.do_update()
    assert ok is True
    assert any("@v1.13.1" in " ".join(c) for c in ran), ran
