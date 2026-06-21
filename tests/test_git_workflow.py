from __future__ import annotations

import subprocess

from ivyea_agent import git_workflow


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "release.yml").write_text("name: Release\non: push\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_status_diff_and_workflows(tmp_path):
    root = _repo(tmp_path)
    st = git_workflow.status(root)
    assert st["ok"] is True
    assert st["clean"] is True
    assert st["head"]

    (root / "pyproject.toml").write_text('[project]\nname = "demo2"\nversion = "0.1.0"\n', encoding="utf-8")
    st = git_workflow.status(root)
    assert st["clean"] is False
    diff = git_workflow.diff_summary(root)
    assert diff["ok"] is True
    assert diff["files"][0]["path"] == "pyproject.toml"
    assert "Git Diff" in git_workflow.render_diff(diff)

    rows = git_workflow.workflows(root)
    assert rows == [{"path": ".github/workflows/release.yml", "name": "Release"}]
    assert "Release" in git_workflow.render_workflows(rows)


def test_release_plan(tmp_path):
    root = _repo(tmp_path)
    plan = git_workflow.release_plan("v0.1.0", root)
    assert plan["ok"] is True
    assert "ready: True" in git_workflow.render_release_plan(plan)

    _git(root, "tag", "v0.1.0")
    plan = git_workflow.release_plan("v0.1.0", root)
    assert plan["ok"] is False
    assert any(c["name"] == "tag not exists" and not c["ok"] for c in plan["checks"])


def test_non_repo(tmp_path):
    assert git_workflow.status(tmp_path)["ok"] is False
    assert git_workflow.release_plan("v1", tmp_path)["ok"] is False
