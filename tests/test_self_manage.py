from __future__ import annotations

import json
import os
import types
import zipfile
from pathlib import Path

from ivyea_agent import self_manage


def test_self_status_and_plans():
    info = self_manage.install_info()
    assert info["version"]
    assert "Ivyea Self Status" in self_manage.render_status(info)

    doctor = self_manage.install_doctor(info)
    assert "checks" in doctor and doctor["next_steps"]
    rendered_doctor = self_manage.render_doctor(doctor)
    assert "Ivyea Install Doctor" in rendered_doctor
    assert "python" in rendered_doctor
    assert "retrieval embeddings" in rendered_doctor

    bootstrap = self_manage.ops_bootstrap()
    assert bootstrap["name"] == "ivyea-agent"
    assert bootstrap["start"]["command"] == "ivyea"
    assert bootstrap["urls"]["manifest"].endswith("/v1/manifest")
    assert bootstrap["urls"]["service_status"].endswith("/v1/system/service/status")
    assert bootstrap["mcp"]["args"] == ["mcp", "serve"]
    assert "systemd_user" in bootstrap["startup_templates"]
    assert bootstrap["service_management"]["log_file"].endswith("ivyea-agent.log")
    assert "IvyeaOps Bootstrap" in self_manage.render_ops_bootstrap(bootstrap)

    upgrade = self_manage.upgrade_plan(version="v1.2.3", method="pipx")
    assert upgrade["action"] == "upgrade"
    assert "v1.2.3" in upgrade["commands"][0]
    assert "Ivyea Self Plan" in self_manage.render_plan(upgrade)

    uninstall = self_manage.uninstall_plan(keep_data=False, method="ivyea-runtime")
    assert uninstall["action"] == "uninstall"
    assert uninstall["keep_data"] is False
    assert uninstall["manual_steps"]
    assert not any("rm -rf" in c for c in uninstall["commands"])


def test_backup(ivyea_home):
    (ivyea_home / "settings.json").write_text("{}", encoding="utf-8")
    (ivyea_home / "skills").mkdir()
    (ivyea_home / "skills" / "note.txt").write_text("skill", encoding="utf-8")

    out = self_manage.backup(ivyea_home / "backup.zip")
    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        assert "settings.json" in zf.namelist()
        assert "skills/note.txt" in zf.namelist()


def test_service_status_logs_and_autostart(ivyea_home, monkeypatch, tmp_path):
    monkeypatch.setattr(self_manage, "_probe_health", lambda *a, **k: {"ok": True, "name": "ivyea-agent"})
    monkeypatch.setattr(self_manage, "_pid_running", lambda pid: pid == 1234)
    (ivyea_home / "run").mkdir(parents=True)
    (ivyea_home / "run" / "ivyea-agent.pid").write_text('{"pid": 1234, "host": "127.0.0.1", "port": 8765}', encoding="utf-8")
    (ivyea_home / "logs").mkdir(parents=True)
    (ivyea_home / "logs" / "ivyea-agent.log").write_text("one\ntwo\nthree\n", encoding="utf-8")

    status = self_manage.service_status()
    assert status["running"] is True
    assert status["pid"] == 1234
    assert "Ivyea Agent Service" in self_manage.render_service_status(status)

    logs = self_manage.service_log_tail(lines=2)
    assert logs["lines"] == ["two", "three"]
    assert "three" in self_manage.render_service_logs(logs)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    written = self_manage.write_autostart(host="127.0.0.1", port=9876)
    assert written["written"] is True
    assert Path(written["target"]).exists()
    assert "9876" in Path(written["target"]).read_text(encoding="utf-8")
    assert "Ivyea Agent Autostart" in self_manage.render_autostart(written)


def test_service_status_ignores_reused_unrelated_pid(ivyea_home, monkeypatch):
    monkeypatch.setattr(self_manage, "_probe_health", lambda *a, **k: {"ok": False, "error": "offline"})
    monkeypatch.setattr(self_manage, "_pid_running", lambda pid: pid == 19)
    monkeypatch.setattr(self_manage, "_pid_cmdline", lambda pid: ["python3", "-m", "unrelated.service"])
    (ivyea_home / "run").mkdir(parents=True)
    (ivyea_home / "run" / "ivyea-agent.pid").write_text('{"pid": 19, "host": "127.0.0.1", "port": 8765}', encoding="utf-8")

    status = self_manage.service_status()
    assert status["running"] is False
    assert status["pid_process_running"] is True
    assert status["pid_matches_service"] is False
    assert status["pid_running"] is False
    assert status["stale_pid"] is True


def test_service_start_stop_with_fake_process(ivyea_home, monkeypatch):
    monkeypatch.setattr(self_manage, "_probe_health", lambda *a, **k: {"ok": False, "error": "offline"})
    monkeypatch.setattr(self_manage, "_pid_running", lambda pid: False)

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.pid = 4321
            self.returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(self_manage.subprocess, "Popen", FakePopen)
    started = self_manage.service_start(wait=False)
    assert started["ok"] is True
    assert started["pid"] == 4321
    assert (ivyea_home / "run" / "ivyea-agent.pid").exists()

    calls = []
    states = iter([True, False])
    monkeypatch.setattr(self_manage, "_pid_running", lambda pid: next(states, False))
    if os.name == "nt":
        # Windows stops via `taskkill` (subprocess.run), not os.kill.
        monkeypatch.setattr(self_manage.subprocess, "run",
                            lambda cmd, *a, **k: types.SimpleNamespace(returncode=0))
    else:
        monkeypatch.setattr(self_manage.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    stopped = self_manage.service_stop(timeout=1)
    assert stopped["ok"] is True
    if os.name != "nt":
        assert calls and calls[0][0] == 4321
    assert not (ivyea_home / "run" / "ivyea-agent.pid").exists()


def test_self_cli_dry_run_and_backup(ivyea_home, capsys):
    from ivyea_agent.cli import main

    assert main(["self", "status"]) == 0
    assert "Ivyea Self Status" in capsys.readouterr().out

    assert main(["self", "doctor"]) == 0
    assert "Ivyea Install Doctor" in capsys.readouterr().out

    assert main(["self", "ops-bootstrap", "--port", "9876"]) == 0
    out = capsys.readouterr().out
    assert "IvyeaOps Bootstrap" in out
    assert "9876" in out

    assert main(["self", "service-logs", "--lines", "5"]) == 0
    assert "Ivyea Agent Service Logs" in capsys.readouterr().out

    assert main(["self", "upgrade", "--method", "pipx", "--version", "v1.2.3"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "v1.2.3" in out

    assert main(["self", "backup", "--output", str(ivyea_home / "backup.zip")]) == 0
    assert "backup.zip" in capsys.readouterr().out


def test_service_stop_finds_a_serve_it_did_not_start(ivyea_home, monkeypatch):
    """pidfile 不可信时按端口找真身，绝不谎报"已停止"。

    实测故障：pidfile 里躺着一个早就死掉的 PID，服务却在正常跑（health 通）。
    旧实现直接 `return already_stopped=True`，于是 `ivyea self service-stop`
    报成功而旧进程原封不动 —— IvyeaOps 的升级流程正是先调它再启新进程，
    结果是"版本号更新了、跑的还是旧代码"，且毫无报错。

    凡是不经 `self service-start` 拉起的 serve 都会命中：systemd 托管的、
    手动起的、setsid 起的孤儿进程。
    """
    from ivyea_agent import self_manage

    (ivyea_home / "run").mkdir(parents=True, exist_ok=True)
    # 陈旧 pidfile：进程早没了
    (ivyea_home / "run" / "ivyea-agent.pid").write_text(
        json.dumps({"pid": 999999, "port": 8765}), encoding="utf-8")

    monkeypatch.setattr(self_manage, "_probe_health", lambda h, p: {"ok": True})
    monkeypatch.setattr(self_manage, "_discover_service_pid", lambda port: 4242)

    killed: list = []
    alive = {4242: True}      # 陈旧的 999999 从来就不在，4242 被杀之后才消失

    monkeypatch.setattr(self_manage, "_pid_running", lambda pid: bool(alive.get(pid)))
    if os.name == "nt":
        monkeypatch.setattr(
            self_manage.subprocess, "run",
            lambda cmd, *a, **k: (killed.append(cmd), alive.pop(4242, None),
                                  types.SimpleNamespace(returncode=0))[-1])
    else:
        monkeypatch.setattr(self_manage.os, "kill",
                            lambda pid, sig: (killed.append(pid), alive.pop(pid, None)))

    res = self_manage.service_stop(timeout=1)
    assert res["discovered_pid"] is True
    assert res["pid"] == 4242
    assert killed, "必须真的去停那个发现出来的进程"


def test_service_stop_refuses_to_lie_when_it_cannot_find_the_process(ivyea_home, monkeypatch):
    """端口还在响应、又找不到进程时，必须报失败而不是"已停止"。"""
    from ivyea_agent import self_manage

    monkeypatch.setattr(self_manage, "_pid_running", lambda pid: False)
    monkeypatch.setattr(self_manage, "_probe_health", lambda h, p: {"ok": True})
    monkeypatch.setattr(self_manage, "_discover_service_pid", lambda port: 0)

    res = self_manage.service_stop(timeout=1)
    assert res["ok"] is False
    assert res["error"] == "service_running_but_pid_unknown"


def test_service_stop_still_reports_already_stopped_when_truly_down(ivyea_home, monkeypatch):
    from ivyea_agent import self_manage

    monkeypatch.setattr(self_manage, "_pid_running", lambda pid: False)
    monkeypatch.setattr(self_manage, "_probe_health", lambda h, p: {"ok": False})
    monkeypatch.setattr(self_manage, "_discover_service_pid", lambda port: 0)

    res = self_manage.service_stop(timeout=1)
    assert res["ok"] is True and res["already_stopped"] is True
