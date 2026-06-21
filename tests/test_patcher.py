from __future__ import annotations

import json
import subprocess

from ivyea_agent import patcher


def test_validate_and_apply_patch(tmp_path):
    f = tmp_path / "app.py"
    f.write_text("value = 1\n", encoding="utf-8")
    spec = patcher.make_spec("app.py", "value = 1", "value = 2")

    validation = patcher.validate_spec(spec, tmp_path)
    assert validation["ok"] is True
    assert "+value = 2" in validation["ops"][0]["diff"]

    dry = patcher.apply_spec(spec, tmp_path, execute=False)
    assert dry["ok"] is True and dry["applied"] is False
    assert f.read_text(encoding="utf-8") == "value = 1\n"

    applied = patcher.apply_spec(spec, tmp_path, execute=True)
    assert applied["ok"] is True and applied["applied"] is True
    assert f.read_text(encoding="utf-8") == "value = 2\n"


def test_validate_rejects_non_unique_old(tmp_path):
    f = tmp_path / "app.py"
    f.write_text("x\nx\n", encoding="utf-8")
    spec = patcher.make_spec("app.py", "x", "y")
    result = patcher.validate_spec(spec, tmp_path)
    assert result["ok"] is False
    assert "实际 2" in result["ops"][0]["message"]


def test_load_spec_and_render(tmp_path):
    path = tmp_path / "patch.json"
    path.write_text(json.dumps({"ops": [{"path": "a.txt", "old": "a", "new": "b"}]}), encoding="utf-8")
    assert patcher.load_spec(path)["ops"][0]["path"] == "a.txt"
    assert "Patch Validation" in patcher.render_validation({"ok": False, "root": str(tmp_path), "ops": []})
    assert "Suggested Tests" in patcher.render_tests(["python -m pytest"])


def test_suggested_tests(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    assert "tests/test_app.py" in patcher.suggested_tests(tmp_path)[0]


def test_patch_cli(tmp_path, capsys):
    from ivyea_agent.cli import main

    f = tmp_path / "a.txt"
    f.write_text("hello\n", encoding="utf-8")
    spec = tmp_path / "patch.json"
    assert main([
        "patch", "make",
        "--path", "a.txt",
        "--old", "hello",
        "--new", "hi",
        "--output", str(spec),
    ]) == 0
    assert spec.exists()

    assert main(["patch", "validate", str(spec), "--root", str(tmp_path)]) == 0
    assert "Patch Validation" in capsys.readouterr().out

    assert main(["patch", "apply", str(spec), "--root", str(tmp_path)]) == 0
    assert f.read_text(encoding="utf-8") == "hello\n"

    assert main(["patch", "apply", str(spec), "--root", str(tmp_path), "--execute", "--yes"]) == 0
    assert f.read_text(encoding="utf-8") == "hi\n"
