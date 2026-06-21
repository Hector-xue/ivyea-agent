from __future__ import annotations

import json

from ivyea_agent import workspace


def test_build_index_skips_noise_and_extracts_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    (tmp_path / "app.py").write_text("class App:\n    pass\n\ndef run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n\nworkspace search target", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")

    idx = workspace.build_index(tmp_path)
    paths = {f["path"] for f in idx["files"]}
    assert "app.py" in paths
    assert "README.md" in paths
    assert ".git/config" not in paths
    app = next(f for f in idx["files"] if f["path"] == "app.py")
    assert app["language"] == "Python"
    assert "App" in app["symbols"]
    assert "run" in app["symbols"]


def test_save_load_search_map_and_explain(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "service.py").write_text(
        "def optimize_campaign():\n    return 'search term budget'\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    idx = workspace.build_index(tmp_path)
    path = workspace.save_index(idx)
    assert path.exists()
    loaded = workspace.load_index(tmp_path)
    assert loaded and loaded["root"] == str(tmp_path)

    rows = workspace.search("campaign", tmp_path)
    assert rows and rows[0]["path"] == "pkg/service.py"

    m = workspace.project_map(tmp_path)
    assert m["file_count"] == 2
    assert "pyproject.toml" in m["important_files"]

    exp_file = workspace.explain("pkg/service.py", tmp_path)
    assert exp_file["kind"] == "file"
    assert "optimize_campaign" in exp_file["summary"]

    exp_dir = workspace.explain("pkg", tmp_path)
    assert exp_dir["kind"] == "directory"
    assert exp_dir["files"] == ["pkg/service.py"]


def test_renderers_are_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "README.md").write_text("hello workspace", encoding="utf-8")
    idx = workspace.build_index(tmp_path)
    out = workspace.render_index(idx)
    assert "Workspace Index" in out
    assert "files: 1" in out
    assert "Workspace Search" in workspace.render_search(workspace.search("hello", tmp_path), "hello")
    assert "Workspace Map" in workspace.render_map(workspace.project_map(tmp_path))
    assert "Workspace Explain" in workspace.render_explain(workspace.explain(".", tmp_path))


def test_index_json_is_serializable(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "a.txt").write_text("abc", encoding="utf-8")
    idx = workspace.build_index(tmp_path)
    assert json.loads(json.dumps(idx, ensure_ascii=False))["files"][0]["path"] == "a.txt"
