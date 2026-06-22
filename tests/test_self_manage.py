from __future__ import annotations

import zipfile

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


def test_self_cli_dry_run_and_backup(ivyea_home, capsys):
    from ivyea_agent.cli import main

    assert main(["self", "status"]) == 0
    assert "Ivyea Self Status" in capsys.readouterr().out

    assert main(["self", "doctor"]) == 0
    assert "Ivyea Install Doctor" in capsys.readouterr().out

    assert main(["self", "upgrade", "--method", "pipx", "--version", "v1.2.3"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "v1.2.3" in out

    assert main(["self", "backup", "--output", str(ivyea_home / "backup.zip")]) == 0
    assert "backup.zip" in capsys.readouterr().out
