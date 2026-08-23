"""飞书配置面：状态、落盘、巡检任务收编。

这些用例守的是三件"配置类功能最容易出的事"：
1. 界面改了没生效（写了文件却没同步生效源）
2. 打开配置页什么都没干、保存一下就把配置清空了
3. 界面上加一条全店巡检，旧的单店任务还在，同一个店被推两遍
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def setup_mod(ivyea_home, monkeypatch):
    from ivyea_agent import config, feishu_client, feishu_setup, schedule

    importlib.reload(schedule)
    importlib.reload(feishu_client)
    importlib.reload(feishu_setup)
    # 环境里可能真有凭据（开发机就有），会让"未配置"的用例假绿
    monkeypatch.delenv(feishu_setup.ENV_APP_ID, raising=False)
    monkeypatch.delenv(feishu_setup.ENV_APP_SECRET, raising=False)
    # systemd 探测在 CI/容器里没有意义，固定成 unknown 让断言只盯配置本身
    monkeypatch.setattr(feishu_setup, "_relay_status",
                        lambda: {"state": "unknown", "running": None, "detail": ""})
    monkeypatch.setattr(feishu_setup, "_timer_status",
                        lambda: {"state": "unknown", "running": None, "detail": ""})
    assert config.IVYEA_DIR == ivyea_home or str(config.IVYEA_DIR) == str(ivyea_home)
    return feishu_setup


def test_status_on_a_fresh_install_is_honest(setup_mod):
    st = setup_mod.status()
    assert st["app"]["configured"] is False
    assert st["channels"]["cards"]["ready"] is False
    assert st["channels"]["approval"]["ready"] is False
    # 一条都没配的时候，向导必须停在第一步
    assert [s["key"] for s in st["steps"] if s["done"]] == []


def test_configure_writes_env_and_process_so_it_takes_effect_now(setup_mod, monkeypatch):
    """写文件不够：serve 是常驻进程，load_env() 又明确不覆盖已有环境变量。

    只写 .env 的话，界面显示新凭据、发出去的还是旧应用——典型的假开关。
    """
    import os

    from ivyea_agent import feishu_client

    setup_mod.configure({"app_id": "cli_new", "app_secret": "s3cret",
                         "chat_id": "oc_target", "domain": "feishu"})
    assert os.environ[setup_mod.ENV_APP_ID] == "cli_new"
    assert feishu_client._creds() == ("cli_new", "s3cret")
    assert feishu_client.default_chat_id() == "oc_target"


def test_configure_ignores_absent_and_blank_fields(setup_mod):
    """界面上没填的框会老实传空串。把空串当"清除"＝打开页面保存一下就瞎。"""
    setup_mod.configure({"app_id": "cli_a", "app_secret": "s", "chat_id": "oc_a"})
    setup_mod.configure({"app_id": "", "app_secret": "", "chat_id": ""})
    st = setup_mod.status()
    assert st["app"]["configured"] is True
    assert st["chat"]["chat_id"] == "oc_a"


def test_clearing_requires_naming_the_field(setup_mod):
    setup_mod.configure({"app_id": "cli_a", "app_secret": "s", "chat_id": "oc_a"})
    setup_mod.configure({"chat_id": "", "clear": ["chat_id"]})
    assert setup_mod.status()["chat"]["chat_id"] == ""


def test_empty_whitelist_is_a_real_revocation(setup_mod):
    """白名单要能删到一个人不剩——这是安全动作，必须有确定路径。"""
    setup_mod.configure({"allowed_senders": ["ou_a", "ou_b"]})
    assert setup_mod.gates()["allowed_senders"] == ["ou_a", "ou_b"]
    setup_mod.configure({"allowed_senders": []})
    assert setup_mod.gates()["allowed_senders"] == []


def test_whitelist_accepts_a_pasted_line(setup_mod):
    setup_mod.configure({"allowed_senders": "ou_a, ou_b\nou_c"})
    assert setup_mod.gates()["allowed_senders"] == ["ou_a", "ou_b", "ou_c"]


def test_changing_credentials_drops_the_cached_token(setup_mod):
    """换了应用还留着旧 token，下一条消息就以旧应用的身份发出去。"""
    from ivyea_agent import feishu_client

    feishu_client._TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    feishu_client._TOKEN_FILE.write_text('{"token": "old"}', encoding="utf-8")
    setup_mod.configure({"app_id": "cli_b", "app_secret": "s2"})
    assert not feishu_client._TOKEN_FILE.exists()


def test_webhook_alone_can_send_text_but_never_cards(setup_mod):
    """只配群机器人 webhook 的人会以为"飞书配好了"，然后奇怪按钮为什么点不了。

    能力矩阵必须把这条差别说出来，否则就是让人去猜。
    """
    setup_mod.configure({"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x"})
    ch = setup_mod.status()["channels"]
    assert ch["text_alert"]["ready"] is True
    assert ch["cards"]["ready"] is False
    assert any("凭据" in b for b in ch["cards"]["blockers"])


def test_approval_needs_whitelist_even_when_everything_else_is_ready(setup_mod, monkeypatch):
    setup_mod.configure({"app_id": "cli_a", "app_secret": "s", "chat_id": "oc_a"})
    monkeypatch.setattr(setup_mod, "_relay_status",
                        lambda: {"state": "active", "running": True, "detail": ""})
    blockers = setup_mod.status()["channels"]["approval"]["blockers"]
    assert any("白名单" in b for b in blockers)


def test_configure_patrol_replaces_hand_made_jobs(setup_mod):
    """界面上开一条全店巡检时，旧的单店任务必须被收编。

    留着的话 timer 下一轮会让同一个店巡两遍、飞书里出现两张几乎一样的卡。
    """
    from ivyea_agent import schedule

    schedule.set_job("l1-1863", "store_l1", args={"sid": "1863", "notify": True,
                                                  "channel": "feishu_app"},
                     every_minutes=20)
    out = setup_mod.configure_patrol({
        "scope": "all",
        "l1": {"enabled": True, "every_minutes": 20},
        "daily": {"enabled": True, "every_hours": 24},
    })
    names = {j["name"] for j in schedule.load()["jobs"]}
    assert names == {"patrol-l1", "patrol-daily"}
    assert out["replaced"] == ["l1-1863"]
    args = next(j["args"] for j in schedule.load()["jobs"] if j["name"] == "patrol-l1")
    assert args["sids"] == "all" and args["channel"] == "feishu_app"


def test_configure_patrol_can_target_specific_stores(setup_mod):
    from ivyea_agent import schedule

    setup_mod.configure_patrol({"scope": "sids", "sids": ["1863", "1872"],
                                "l2": {"enabled": True, "every_hours": 1}})
    args = next(j["args"] for j in schedule.load()["jobs"] if j["name"] == "patrol-l2")
    assert args["sids"] == ["1863", "1872"]


def test_configure_patrol_refuses_an_empty_store_selection(setup_mod):
    out = setup_mod.configure_patrol({"scope": "sids", "sids": [],
                                      "l1": {"enabled": True, "every_minutes": 20}})
    assert out["ok"] is False


def test_turning_everything_off_leaves_no_patrol_job(setup_mod):
    """测试店铺跑完就不想要巡检了 —— 关掉必须是真关掉，不留残留任务。"""
    from ivyea_agent import schedule

    setup_mod.configure_patrol({"scope": "all", "l1": {"enabled": True, "every_minutes": 20}})
    setup_mod.configure_patrol({"scope": "all"})
    assert [j for j in schedule.load()["jobs"] if j["task"].startswith("store_")] == []
    assert setup_mod.status()["channels"]["patrol_push"]["ready"] is False


def test_send_test_records_only_on_success(setup_mod, monkeypatch):
    from ivyea_agent import feishu_client

    setup_mod.configure({"app_id": "cli_a", "app_secret": "s", "chat_id": "oc_a"})

    def _boom(*a, **k):
        raise feishu_client.FeishuError("飞书接口错误 code=99991672 msg=权限不足", 99991672)

    monkeypatch.setattr(feishu_client, "send_card", _boom)
    out = setup_mod.send_test({})
    assert out["ok"] is False and setup_mod.status()["last_test_at"] == 0

    monkeypatch.setattr(feishu_client, "send_card", lambda chat, card: "om_1")
    out = setup_mod.send_test({})
    assert out["ok"] is True and setup_mod.status()["last_test_at"] > 0


def test_send_test_without_a_target_says_so(setup_mod):
    setup_mod.configure({"app_id": "cli_a", "app_secret": "s"})
    out = setup_mod.send_test({})
    assert out["ok"] is False and "会话" in out["error"]
