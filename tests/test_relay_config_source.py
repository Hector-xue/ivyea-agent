"""白名单来源的优先级 —— agent 配置优先，env 兜底。

这条规则安全攸关：搞反了，"在 IvyeaOps 界面上把人从审批白名单里删掉"
就会变成一个假动作（界面上没了，relay 还认着 env 里的旧名单放行）。
"""
import importlib
import json

import pytest


from ivyea_agent.feishu_relay import config


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """把 ~/.ivyea 指到临时目录。**绝不能让单测读到真机的白名单。**"""
    monkeypatch.setenv("IVYEA_HOME", str(tmp_path))
    monkeypatch.delenv("ALLOWED_SENDER_IDS", raising=False)
    monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)
    return tmp_path


def _write(dirpath, settings):
    (dirpath / "settings.json").write_text(json.dumps(settings, ensure_ascii=False),
                                           encoding="utf-8")


def test_agent_settings_win_over_env(fake_home, monkeypatch):
    monkeypatch.setenv("ALLOWED_SENDER_IDS", "ou_old")
    _write(fake_home, {"feishu_allowed_senders": ["ou_new"]})
    assert config.allowed_sender_ids() == {"ou_new"}


def test_empty_list_in_settings_revokes_everyone(fake_home, monkeypatch):
    """空列表是「谁都不许点」这条**有意义的配置**，不是「没配、去读 env」。

    撤权必须撤得干净：这里若回退到 env，界面上删光了人，按钮照样点得动。
    """
    monkeypatch.setenv("ALLOWED_SENDER_IDS", "ou_old")
    _write(fake_home, {"feishu_allowed_senders": []})
    assert config.allowed_sender_ids() == set()


def test_env_used_when_settings_never_configured(fake_home, monkeypatch):
    """老部署（只有 systemd EnvironmentFile、从没用过界面）必须原样能跑。"""
    monkeypatch.setenv("ALLOWED_SENDER_IDS", "ou_a, ou_b")
    _write(fake_home, {"feishu_domain": "feishu"})
    assert config.allowed_sender_ids() == {"ou_a", "ou_b"}


def test_missing_settings_file_is_not_an_error(fake_home, monkeypatch):
    monkeypatch.setenv("ALLOWED_SENDER_IDS", "ou_a")
    assert config.allowed_sender_ids() == {"ou_a"}


def test_broken_settings_file_falls_back_instead_of_crashing(fake_home, monkeypatch):
    """relay 崩掉 = 飞书那头彻底没人接，比读不到配置严重得多。"""
    monkeypatch.setenv("ALLOWED_SENDER_IDS", "ou_a")
    (fake_home / "settings.json").write_text("{ 这不是 json", encoding="utf-8")
    assert config.allowed_sender_ids() == {"ou_a"}


def test_comma_or_newline_separated_string_is_accepted(fake_home):
    """界面上的输入框是一行文本，别人手写配置又爱换行分隔，两种都要认。"""
    _write(fake_home, {"feishu_allowed_senders": "ou_a,\nou_b"})
    assert config.allowed_sender_ids() == {"ou_a", "ou_b"}


def test_chat_whitelist_follows_the_same_rule(fake_home, monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "oc_old")
    _write(fake_home, {"feishu_allowed_chats": ["oc_new"]})
    assert config.allowed_chat_ids() == {"oc_new"}


def test_empty_whitelist_no_longer_blocks_startup(monkeypatch):
    """启动的硬门槛只有凭据；白名单为空只是警告。

    把关仍在 gates.sender_allowed（空名单拒绝所有人），这里放行的只是
    "填完凭据先起来试试对话"这个配置期的死结。
    """
    monkeypatch.setattr(config, "FEISHU_APP_ID", "cli_x")
    monkeypatch.setattr(config, "FEISHU_APP_SECRET", "s")
    monkeypatch.setattr(config, "ALLOWED_SENDER_IDS", set())
    assert config.missing() == []
    assert config.warnings()


def test_reimport_does_not_read_the_real_home(fake_home):
    """模块级快照也要跟着 IVYEA_HOME 走，不能焊死 ~/.ivyea。"""
    _write(fake_home, {"feishu_allowed_senders": ["ou_snapshot"]})
    reloaded = importlib.reload(config)
    try:
        assert reloaded.ALLOWED_SENDER_IDS == {"ou_snapshot"}
    finally:
        importlib.reload(config)
