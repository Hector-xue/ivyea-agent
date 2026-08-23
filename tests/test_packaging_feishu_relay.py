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
