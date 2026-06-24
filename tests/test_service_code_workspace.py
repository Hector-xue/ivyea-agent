from __future__ import annotations

import json
import subprocess
import threading
import urllib.request

import pytest


def _make_project(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "calc.py").write_text(
        "def add(left, right):\n"
        "    return left + right\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    return tmp_path


def test_service_workspace_and_code_helpers(ivyea_home, tmp_path):
    from ivyea_agent import service

    root = _make_project(tmp_path)
    indexed = service.workspace_index({"root": str(root)})
    assert indexed["ok"] is True
    assert indexed["workspace"]["file_count"] >= 3
    assert indexed["workspace"]["languages"]["Python"] >= 2

    found = service.workspace_search({"root": str(root), "query": "add", "limit": 5})
    assert any(row["path"] == "pkg/calc.py" for row in found["results"])

    inspected = service.workspace_inspect({"root": str(root)})
    assert inspected["inspect"]["tests"] == ["tests/test_calc.py"]

    symbols = service.workspace_symbols({"root": str(root), "query": "add"})
    assert any(row["path"] == "pkg/calc.py" for row in symbols["symbols"])

    impact = service.workspace_impact({"root": str(root), "target": "add"})
    assert any(row["path"] == "pkg/calc.py" for row in impact["definitions"])
    assert "tests/test_calc.py" in impact["tests"]

    plan = service.code_plan({"root": str(root), "goal": "change add behavior"})
    assert "pkg/calc.py" in plan["plan"]["relevant_files"]
    assert "dry-run" in plan["plan"]["write_policy"]

    ctx = service.code_context({"root": str(root), "goal": "change add behavior"})
    assert any(row["path"] == "pkg/calc.py" for row in ctx["context"]["files"])

    bundle = service.code_bundle({
        "root": str(root),
        "goal": "change add behavior",
        "output": "FAILED tests/test_calc.py::test_add - AssertionError: expected sum",
    })
    assert bundle["ok"] is True
    assert bundle["bundle"]["mode"] == "read-only-task-bundle"
    assert bundle["bundle"]["repair"]["failure_summary"][0]["kind"] == "assertion"

    loop = service.code_apply_loop({
        "root": str(root),
        "spec": {
            "ops": [{
                "path": "pkg/calc.py",
                "old": "    return left + right\n",
                "new": "    return left + right + 0\n",
            }]
        },
        "execute": False,
        "persist": False,
    })
    assert loop["ok"] is True
    assert loop["run"]["patch"]["status"] == "valid"
    assert loop["run"]["patch"]["apply"]["applied"] is False

    quality = service.code_quality({"root": str(root)})
    assert quality["quality"]["file_count"] >= 3

    review = service.code_review({"root": str(root)})
    assert "review" in review["review"]

    repair = service.code_repair({
        "root": str(root),
        "output": "FAILED tests/test_calc.py::test_add - AssertionError: expected sum",
    })
    assert repair["repair"]["failure_count"] == 1
    assert repair["repair"]["failure_summary"][0]["kind"] == "assertion"


def test_service_workspace_and_code_http_routes(ivyea_home, tmp_path):
    from ivyea_agent import service

    root = _make_project(tmp_path)
    try:
        server = service.make_server("127.0.0.1", 0)
    except PermissionError:
        pytest.skip("local socket binding is not available in this sandbox")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/v1/workspace/index",
            data=json.dumps({"root": str(root)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            indexed = json.loads(resp.read().decode("utf-8"))
        assert indexed["ok"] is True
        assert indexed["workspace"]["file_count"] >= 3

        req = urllib.request.Request(
            f"http://{host}:{port}/v1/code/plan",
            data=json.dumps({"root": str(root), "goal": "change add behavior"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            plan = json.loads(resp.read().decode("utf-8"))
        assert plan["ok"] is True
        assert "pkg/calc.py" in plan["plan"]["relevant_files"]

        req = urllib.request.Request(
            f"http://{host}:{port}/v1/code/bundle",
            data=json.dumps({"root": str(root), "goal": "change add behavior"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            bundle = json.loads(resp.read().decode("utf-8"))
        assert bundle["ok"] is True
        assert bundle["bundle"]["mode"] == "read-only-task-bundle"

        req = urllib.request.Request(
            f"http://{host}:{port}/v1/code/apply-loop",
            data=json.dumps({
                "root": str(root),
                "spec": {"ops": [{
                    "path": "pkg/calc.py",
                    "old": "    return left + right\n",
                    "new": "    return left + right + 0\n",
                }]},
                "execute": False,
                "persist": False,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            loop = json.loads(resp.read().decode("utf-8"))
        assert loop["ok"] is True
        assert loop["run"]["patch"]["status"] == "valid"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
