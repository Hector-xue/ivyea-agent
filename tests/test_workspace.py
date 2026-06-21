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
    assert app["imports"] == []
    assert {"name": "App", "kind": "class", "qualname": "App", "lineno": 1, "end_lineno": 2} in app["definitions"]
    assert any(d["qualname"] == "run" and d["lineno"] == 4 for d in app["definitions"])


def test_python_ast_index_extracts_methods_and_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "svc.py").write_text(
        "import json\n\n"
        "class Service:\n"
        "    async def fetch(self):\n"
        "        return json.loads('{}')\n\n"
        "def build():\n"
        "    svc = Service()\n"
        "    return svc.fetch()\n",
        encoding="utf-8",
    )

    idx = workspace.build_index(tmp_path)
    svc = next(f for f in idx["files"] if f["path"] == "svc.py")
    assert any(d["qualname"] == "Service.fetch" and d["kind"] == "async_method" for d in svc["definitions"])
    assert any(d["qualname"] == "build" and d["kind"] == "function" for d in svc["definitions"])
    assert "json.loads" in svc["calls"]
    assert "Service" in svc["calls"]


def test_javascript_typescript_semantic_index(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "ui.ts").write_text(
        "export class Panel {}\n\n"
        "export function renderPanel() {\n"
        "  return formatTitle('x')\n"
        "}\n\n"
        "export const formatTitle = (value: string) => value.toUpperCase()\n",
        encoding="utf-8",
    )

    idx = workspace.build_index(tmp_path)
    ui = next(f for f in idx["files"] if f["path"] == "ui.ts")
    assert ui["language"] == "TypeScript"
    assert any(d["qualname"] == "Panel" and d["kind"] == "class" for d in ui["definitions"])
    assert any(d["qualname"] == "renderPanel" for d in ui["definitions"])
    assert any(d["qualname"] == "formatTitle" for d in ui["definitions"])
    assert "formatTitle" in ui["calls"]

    workspace.save_index(idx)
    symbols = workspace.symbol_index(tmp_path, query="renderPanel")
    assert any(s["path"] == "ui.ts" for s in symbols["symbols"])


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
    assert "optimize_campaign:1" in exp_file["summary"]

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


def test_dependency_graph_and_project_inspect(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text("import json\nfrom pkg.util import helper\n\ndef run():\n    return helper()\n", encoding="utf-8")
    (tmp_path / "pkg" / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_core.py").write_text("from pkg.core import run\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='demo'\n\n[project.scripts]\ndemo='pkg.core:run'\n",
        encoding="utf-8",
    )

    idx = workspace.build_index(tmp_path)
    workspace.save_index(idx)
    core = next(f for f in idx["files"] if f["path"] == "pkg/core.py")
    assert "json" in core["imports"]
    assert "pkg.util" in core["imports"]

    graph = workspace.dependency_graph(tmp_path)
    assert graph["edge_count"] >= 1
    assert any(e["from"] == "pkg/core.py" and e["to"] == "pkg/util.py" for e in graph["edges"])
    assert "json" in graph["external"]
    assert "Workspace Graph" in workspace.render_graph(graph)

    inspected = workspace.project_inspect(tmp_path)
    assert any(e.get("name") == "demo" for e in inspected["entrypoints"])
    assert "tests/test_core.py" in inspected["tests"]
    assert "python -m pytest" in inspected["suggested_commands"]
    assert "ivyea gitops ci --root ." in inspected["suggested_commands"]
    assert "Workspace Inspect" in workspace.render_inspect(inspected)


def test_symbol_index_and_impact_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "calc.py").write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")
    (tmp_path / "pkg" / "service.py").write_text("from pkg.calc import add\n\ndef total():\n    return add(1, 2)\n", encoding="utf-8")
    (tmp_path / "tests" / "test_calc.py").write_text("from pkg.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")

    idx = workspace.build_index(tmp_path)
    workspace.save_index(idx)
    symbols = workspace.symbol_index(tmp_path, query="add")
    assert any(s["path"] == "pkg/calc.py" and s["qualname"] == "add" for s in symbols["symbols"])
    assert "Workspace Symbols" in workspace.render_symbols(symbols)

    impact = workspace.impact_analysis("add", tmp_path)
    assert any(d["path"] == "pkg/calc.py" for d in impact["definitions"])
    assert any(c["path"] == "pkg/service.py" for c in impact["callers"])
    assert "tests/test_calc.py" in impact["tests"]
    assert impact["suggested_tests"] == ["python -m pytest tests/test_calc.py"]
    assert "Workspace Impact" in workspace.render_impact(impact)


def test_index_json_is_serializable(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
    (tmp_path / "a.txt").write_text("abc", encoding="utf-8")
    idx = workspace.build_index(tmp_path)
    assert json.loads(json.dumps(idx, ensure_ascii=False))["files"][0]["path"] == "a.txt"
