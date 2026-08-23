"""serve 进程内的两个常驻工人。

它们存在的意义是"用户不用再装两个系统服务"。而它们最危险的失败模式不是没启动，
是**和外部服务同时启动**：两条飞书长连接 = 同一次按钮点击可能被执行两遍
（去重表是进程内的，两个进程互相不认）；两个节拍器 = 同一份早报推两遍。
"""
from __future__ import annotations

import threading


def _reload(monkeypatch):
    import importlib

    from ivyea_agent import serve_workers
    importlib.reload(serve_workers)
    return serve_workers


def test_scheduler_stands_down_when_the_system_timer_is_running(ivyea_home, monkeypatch):
    sw = _reload(monkeypatch)
    monkeypatch.setattr(sw, "_external_timer_running", lambda: True)
    monkeypatch.setattr(sw, "_external_relay_running", lambda: True)
    out = sw.start_all(threading.Event())
    assert out["scheduler"]["started"] is False
    assert "不重复" in out["scheduler"]["reason"]


def test_relay_stands_down_when_the_standalone_service_is_running(ivyea_home, monkeypatch):
    from ivyea_agent import feishu_client, feishu_relay

    sw = _reload(monkeypatch)
    monkeypatch.setattr(sw, "_external_timer_running", lambda: True)
    monkeypatch.setattr(sw, "_external_relay_running", lambda: True)
    monkeypatch.setattr(feishu_client, "is_configured", lambda: True)
    monkeypatch.setattr(feishu_relay, "sdk_available", lambda: True)
    out = sw.start_all(threading.Event())
    assert out["relay"]["started"] is False and "不重复" in out["relay"]["reason"]


def test_scheduler_starts_when_nothing_else_is_running(ivyea_home, monkeypatch):
    sw = _reload(monkeypatch)
    monkeypatch.setattr(sw, "_external_timer_running", lambda: False)
    monkeypatch.setattr(sw, "_external_relay_running", lambda: True)
    stop = threading.Event()
    try:
        out = sw.start_all(stop)
        assert out["scheduler"]["started"] is True
    finally:
        stop.set()


def test_each_not_started_reason_is_distinguishable(ivyea_home, monkeypatch):
    """只报"未运行"的话，用户无从判断该去装什么还是该去配什么。"""
    from ivyea_agent import feishu_client, feishu_relay

    sw = _reload(monkeypatch)
    monkeypatch.setattr(sw, "_external_timer_running", lambda: True)
    monkeypatch.setattr(sw, "_external_relay_running", lambda: False)

    monkeypatch.setattr(feishu_client, "is_configured", lambda: False)
    assert "凭据" in sw.start_all(threading.Event())["relay"]["reason"]

    monkeypatch.setattr(feishu_client, "is_configured", lambda: True)
    monkeypatch.setattr(feishu_relay, "sdk_available", lambda: False)
    assert "pip install" in sw.start_all(threading.Event())["relay"]["reason"]


def test_off_switch_is_honoured(ivyea_home, monkeypatch):
    from ivyea_agent import config

    sw = _reload(monkeypatch)
    config.set_setting("serve_worker_scheduler", "off")
    config.set_setting("serve_worker_relay", "off")
    out = sw.start_all(threading.Event())
    assert out["scheduler"]["started"] is False and out["relay"]["started"] is False
    assert "关闭" in out["scheduler"]["reason"]


def test_only_one_serve_process_ticks(ivyea_home, monkeypatch):
    """多开 serve 时用文件锁选一个。两个都跑 = 早报推两遍。"""
    sw = _reload(monkeypatch)
    assert sw._claim() is True
    monkeypatch.setattr(sw.os, "getpid", lambda: 999999)
    assert sw._claim() is False, "另一个进程不该同时抢到"


def test_a_stale_lock_can_be_taken_over(ivyea_home, monkeypatch):
    """进程被 kill -9 不会留下清理机会。锁若永不过期，重启后这台机器就再也
    没有节拍器了，而且毫无征兆。"""
    import json
    import time

    sw = _reload(monkeypatch)
    sw._LOCK_FILE.write_text(json.dumps(
        {"pid": 123456, "ts": time.time() - sw._LOCK_TTL - 60}), encoding="utf-8")
    assert sw._claim() is True


def test_builtin_relay_counts_as_installed(ivyea_home, monkeypatch):
    """进程内长连接就是接收端。界面若还催用户去装第二个，那是在制造重复。"""
    from ivyea_agent import feishu_setup

    sw = _reload(monkeypatch)
    sw._note("relay", running=True)
    st = feishu_setup._relay_status()
    assert st["running"] is True and st.get("builtin") is True


def test_builtin_scheduler_counts_as_installed(ivyea_home, monkeypatch):
    from ivyea_agent import host_services

    sw = _reload(monkeypatch)
    monkeypatch.setattr(host_services, "_systemd", lambda: True)
    sw._note("scheduler", running=True)
    st = host_services.schedule_status()
    assert st["running"] is True and st.get("builtin") is True


def test_worker_state_is_visible_from_other_processes(ivyea_home, monkeypatch):
    """工人活在 serve 进程里，而 `ivyea relay status`、doctor、IvyeaOps 自检
    都是**别的进程**在问。只存内存的话它们会一致地报"没在跑"，
    然后催用户去装一个其实不需要的服务。"""
    import json

    sw = _reload(monkeypatch)
    sw._note("relay", running=True)
    assert sw._STATE_FILE.exists()
    on_disk = json.loads(sw._STATE_FILE.read_text(encoding="utf-8"))
    assert on_disk["relay"]["running"] is True
    # 模拟"另一个进程"：内存是空的，只能读盘
    sw2 = _reload(monkeypatch)
    assert sw2.status()["relay"]["running"] is True


def test_a_dead_serve_does_not_keep_claiming_it_runs(ivyea_home, monkeypatch):
    """serve 被 kill -9 时没机会清理文件。心跳过期就必须当它没了，
    否则界面永远显示"内建运行中"，而实际上谁都没在接。"""
    import json
    import time

    sw = _reload(monkeypatch)
    sw._STATE_FILE.write_text(json.dumps(
        {"relay": {"running": True, "ts": time.time() - sw._STATE_TTL - 60}}),
        encoding="utf-8")
    sw2 = _reload(monkeypatch)
    assert sw2.status().get("relay") is None


def test_turning_a_worker_off_clears_the_stale_running_flag(ivyea_home, monkeypatch):
    """关掉之后还显示"内建运行中"，比一开始就没启动更糟。"""
    import threading

    from ivyea_agent import config

    sw = _reload(monkeypatch)
    sw._note("scheduler", running=True)
    config.set_setting("serve_worker_scheduler", "off")
    sw.start_all(threading.Event())
    assert sw.status()["scheduler"]["running"] is False
