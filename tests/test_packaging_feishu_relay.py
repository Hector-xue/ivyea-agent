"""飞书接收端必须真的进 wheel。

漏了不会报错，表现是**用户点卡片按钮什么都不发生** —— 出站（发卡片）在 agent 本体，
入站（收回调、飞书对话）靠这条长连接。它曾经是一个独立目录、不随任何 release 发出去，
于是拿到开源包的人：卡片收得到、按钮点了没反应、飞书里也没法对话，
而界面上那个按钮还看得见、点得动。比没有按钮更糟。

一个别人用不了的功能，开源出去没有意义。这件事必须有测试压住。
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RELAY_MODULES = ("relay", "config", "gates", "handlers", "chat", "agent_client",
                 "__main__")


def test_relay_lives_inside_the_package():
    d = REPO / "ivyea_agent" / "feishu_relay"
    assert d.is_dir(), "接收端不在包里 —— 那它就不会随 wheel 发出去"
    for m in RELAY_MODULES:
        assert (d / f"{m}.py").exists(), f"缺 {m}.py"


def test_relay_has_no_top_level_imports_of_its_siblings():
    """独立目录时代靠 sys.path 生效的 `import config`，装进 site-packages 后
    会去撞用户环境里任何一个叫 config 的模块 —— 要么 ImportError，要么更糟：
    导到别人的模块上。"""
    d = REPO / "ivyea_agent" / "feishu_relay"
    for p in d.glob("*.py"):
        for line in p.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            for sib in ("config", "gates", "handlers", "chat", "agent_client"):
                assert stripped != f"import {sib}", f"{p.name} 里还有裸 `import {sib}`"


def test_state_dir_is_not_inside_the_installed_package():
    """会话映射默认落 ~/.ivyea。落在模块目录下的话，root 装、普通用户跑就直接崩。"""
    from ivyea_agent.feishu_relay import config

    assert "site-packages" not in config.STATE_DIR
    assert ".ivyea" in config.STATE_DIR


def test_sdk_is_optional_but_the_hint_is_actionable():
    """SDK 42MB / 一万个文件，不用飞书的人不该为它买单；但缺它时必须给出
    **能直接敲的命令**，只说"缺依赖"等于让人自己猜包名。"""
    from ivyea_agent import feishu_relay

    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "feishu = [" in text and "lark-oapi" in text, "pyproject 里没有 feishu extra"
    assert "pip install" in feishu_relay.SDK_HINT
    assert "ivyea-agent[feishu]" in feishu_relay.SDK_HINT


def test_missing_relay_tells_the_user_how_to_install_it():
    """配置向导第 5 步不能只说"未安装" —— 用户装不了就等于功能不存在。"""
    from ivyea_agent import feishu_setup

    step = next(s for s in feishu_setup.status()["steps"] if s["key"] == "relay")
    assert "ivyea relay install" in step["hint"]


def test_legacy_service_name_is_still_recognised():
    """本机手工部署时用的是旧单元名。升级后若只认新名，会把一个跑得好好的服务
    显示成"未安装"，然后用户去装第二份。"""
    from ivyea_agent import feishu_setup

    assert "feishu-ivyea-relay.service" in feishu_setup.LEGACY_RELAY_SERVICES


@pytest.mark.slow
def test_built_wheel_contains_the_relay(tmp_path):
    """真构一次 wheel 并翻开看 —— 目录建对了不等于构出来就有。"""
    pytest.importorskip("build")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=str(REPO), capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, f"构建失败：{proc.stderr[-2000:]}"
    wheel = next(iter(tmp_path.glob("*.whl")))
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
    for m in RELAY_MODULES:
        assert f"ivyea_agent/feishu_relay/{m}.py" in names, f"wheel 里没有 {m}.py"


# ── 巡检触发器同样必须随包走 ─────────────────────────────────────────────────
# 它以前只以 deploy/systemd/*.timer 的形式存在于仓库里，wheel 里没有。
# 后果比 relay 更隐蔽：用户在界面上把巡检开关全打开，界面显示"已启用"，
# 然后**永远不触发**，也没有任何报错——因为缺的东西根本不在他机器上。

def test_timer_units_are_generated_from_code_not_read_from_repo():
    from ivyea_agent import host_services

    assert "ExecStart=" in host_services._SCHEDULE_SERVICE_UNIT
    assert "OnUnitActiveSec=5min" in host_services._SCHEDULE_TIMER_UNIT
    assert "Persistent=true" in host_services._SCHEDULE_TIMER_UNIT, \
        "停机期间错过的执行必须补跑，否则关机一晚上等于漏一晚上巡检"
    # 单元内容不能靠读仓库文件——pip 装的用户没有 deploy/ 目录
    src = (REPO / "ivyea_agent" / "host_services.py").read_text(encoding="utf-8")
    assert "deploy/systemd" not in src


def test_schedule_status_says_registered_is_not_running(monkeypatch):
    """"注册了 3 个任务"和"这 3 个任务会被执行"是两回事，措辞必须区分。"""
    from ivyea_agent import host_services

    monkeypatch.setattr(host_services, "_systemd", lambda: True)
    monkeypatch.setattr(host_services, "_unit_active", lambda name: "inactive")
    st = host_services.schedule_status()
    assert st["running"] is False and st["can_install"] is True
    assert "不会被触发" in st["detail"]


def test_install_actions_are_reachable_from_the_web(monkeypatch):
    """网页用户没有终端。装这两样必须能从界面点，否则等于功能不存在。"""
    from ivyea_agent import service

    calls = []
    monkeypatch.setattr("ivyea_agent.host_services.install_relay",
                        lambda **k: calls.append("relay") or {"ok": True})
    monkeypatch.setattr("ivyea_agent.host_services.install_schedule",
                        lambda: calls.append("timer") or {"ok": True})
    assert service.feishu_config_action({"action": "install_relay"})[0] == 200
    assert service.feishu_config_action({"action": "install_timer"})[0] == 200
    assert calls == ["relay", "timer"]


def test_relay_install_pulls_the_sdk_when_missing(monkeypatch, tmp_path):
    """"请自行 pip install" 对网页用户等于"这个功能你用不了"。"""
    from ivyea_agent import feishu_relay, host_services

    ran = []
    monkeypatch.setattr(feishu_relay, "sdk_available", lambda: bool(ran))
    monkeypatch.setattr(host_services, "_systemd", lambda: False)
    monkeypatch.setattr(host_services, "_run",
                        lambda cmd, timeout=30.0: ran.append(cmd) or
                        {"cmd": " ".join(cmd), "ok": True, "detail": ""})
    out = host_services.install_relay()
    assert any("lark-oapi>=1.4" in " ".join(c) for c in ran), "没去装 SDK"
    assert out["manual"] is True and "-m ivyea_agent.feishu_relay" in out["hint"]
