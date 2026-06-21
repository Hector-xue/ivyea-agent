from __future__ import annotations

import subprocess

from ivyea_agent import code_review


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_review_detects_secret_and_missing_tests(tmp_path):
    root = _repo(tmp_path)
    (root / "app.py").write_text(
        'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz"\n\ndef ok():\n    return 2\n',
        encoding="utf-8",
    )
    result = code_review.review_diff(root)
    assert result["ok"] is True
    assert any(f["severity"] == "high" and "密钥" in f["title"] for f in result["findings"])
    assert any("未看到测试" in f["title"] for f in result["findings"])
    out = code_review.render(result)
    assert "Code Review" in out
    assert "findings:" in out


def test_review_detects_shell_true_and_broad_except(tmp_path):
    root = _repo(tmp_path)
    (root / "app.py").write_text(
        "import subprocess\ntry:\n    subprocess.run('ls', shell=True)\nexcept Exception:\n    pass\n",
        encoding="utf-8",
    )
    result = code_review.review_diff(root)
    titles = {f["title"] for f in result["findings"]}
    assert "新增危险命令执行模式" in titles
    assert "新增过宽异常捕获" in titles


def test_review_no_findings_when_tests_changed(tmp_path):
    root = _repo(tmp_path)
    (root / "app.py").write_text("def ok():\n    return 2\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_ok():\n    assert 2 == 2\n", encoding="utf-8")
    result = code_review.review_diff(root)
    assert result["findings"] == []
    assert "未发现" in code_review.render(result)


def test_review_scans_untracked_files(tmp_path):
    root = _repo(tmp_path)
    (root / "new_module.py").write_text("TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz'\n", encoding="utf-8")
    result = code_review.review_diff(root)
    assert "new_module.py" in result["files"]
    assert any(f["path"] == "new_module.py" and f["severity"] == "high" for f in result["findings"])
