from __future__ import annotations

import json


def test_policy_default_and_init(ivyea_home):
    from ivyea_agent import policy

    assert policy.check_command("echo ok") == (True, "")
    created, path = policy.init()
    assert created is True
    assert str(ivyea_home / "policy.json") == path
    assert "Ivyea Policy" in policy.render()


def test_policy_path_and_command_rules(ivyea_home, tmp_path):
    from ivyea_agent import policy

    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    policy.POLICY_FILE.write_text(json.dumps({
        "file_read_roots": [str(allowed)],
        "file_write_roots": [str(allowed)],
        "command_allow": ["echo *"],
        "command_deny": ["echo secret*"],
        "block_dangerous_commands": True,
    }), encoding="utf-8")

    assert policy.check_path(allowed / "a.txt", "read")[0] is True
    assert policy.check_path(denied / "a.txt", "read")[0] is False
    assert policy.check_command("echo ok")[0] is True
    assert policy.check_command("echo secret key")[0] is False
    assert policy.check_command("python -V")[0] is False


def test_policy_command_assessment(ivyea_home):
    from ivyea_agent import policy

    low = policy.assess_command("git status --short")
    assert low["ok"] is True
    assert low["risk"] == "low"

    med = policy.assess_command("git push origin main")
    assert med["ok"] is True
    assert med["risk"] == "medium"

    high = policy.assess_command("rm -rf /")
    assert high["ok"] is False
    assert high["risk"] == "blocked"
    assert "Ivyea Command Policy" in policy.render_command_assessment("git status")


def test_policy_cli_and_tools(ivyea_home, tmp_path, capsys):
    from ivyea_agent import policy, tools_general as tg
    from ivyea_agent.agent_tools import ToolContext
    from ivyea_agent.cli import main

    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    ok_file = allowed / "ok.txt"
    ok_file.write_text("ok", encoding="utf-8")
    bad_file = denied / "bad.txt"
    bad_file.write_text("bad", encoding="utf-8")
    policy.POLICY_FILE.write_text(json.dumps({
        "file_read_roots": [str(allowed)],
        "file_write_roots": [str(allowed)],
        "command_deny": ["git reset --hard*"],
    }), encoding="utf-8")

    assert "ok" in tg.t_read_file({"path": str(ok_file)}, ToolContext())
    assert "policy 拒绝" in tg.t_read_file({"path": str(bad_file)}, ToolContext())
    assert "policy 拒绝" in tg.t_run_command({"command": "git reset --hard"}, ToolContext())

    assert main(["policy", "show"]) == 0
    assert "Ivyea Policy" in capsys.readouterr().out
    assert main(["policy", "check-path", str(ok_file)]) == 0
    assert "OK" in capsys.readouterr().out
    assert main(["policy", "explain-command", "git status --short"]) == 0
    assert "risk: low" in capsys.readouterr().out
