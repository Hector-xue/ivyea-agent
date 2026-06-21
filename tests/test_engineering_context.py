from __future__ import annotations

from ivyea_agent import engineering_context


def test_should_include_engineering_terms():
    assert engineering_context.should_include("继续优化这个项目的测试")
    assert engineering_context.should_include("fix release workflow")
    assert not engineering_context.should_include("分析这个亚马逊搜索词报告")


def test_build_engineering_context(tmp_path, monkeypatch):
    from ivyea_agent import workspace

    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (tmp_path / "tests" / "test_cli.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    text = engineering_context.build(tmp_path, "继续优化这个项目")
    assert "[workspace]" in text
    assert "tests/test_cli.py" in text
    assert "[skills]" in text

    assert engineering_context.build(tmp_path, "看一下广告否词") == ""


def test_repo_conventions_extracts_project_instructions(tmp_path, monkeypatch):
    from ivyea_agent import workspace

    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# 约定\n\n提交前运行 python -m pytest。\n不要自动 push。\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n\n## Test\n\npython -m pytest\n", encoding="utf-8")

    conventions = engineering_context.repo_conventions(tmp_path)
    assert conventions["files"][0]["path"] == "AGENTS.md"
    assert "不要自动 push" in conventions["files"][0]["summary"]
    assert "python -m pytest" in conventions["suggested_commands"]
