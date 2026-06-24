from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ivyea_agent import code_agent, workspace


def _make_project(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACE_DIR", tmp_path / ".ivyea" / "workspaces")
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


def test_task_plan_and_context_use_workspace_semantics(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    (root / "AGENTS.md").write_text("# 约定\n\n提交前运行 python -m pytest，不要自动 push。\n", encoding="utf-8")

    plan = code_agent.task_plan("change add behavior", root=root)
    assert plan["root"] == str(root)
    assert "pkg/calc.py" in plan["relevant_files"]
    assert "python -m pytest" in plan["suggested_tests"]
    assert "dry-run" in plan["write_policy"]
    assert any(item["path"] == "AGENTS.md" for item in plan["conventions"]["files"])

    ctx = code_agent.context("change add behavior", root=root)
    calc = next(f for f in ctx["files"] if f["path"] == "pkg/calc.py")
    assert any(d["qualname"] == "add" for d in calc["definitions"])
    assert "def add" in calc["preview"]
    assert "Code Plan" in code_agent.render_plan(plan)
    assert "Repo Conventions" in code_agent.render_plan(plan)
    assert "Code Context" in code_agent.render_context(ctx)
    assert "不要自动 push" in code_agent.render_context(ctx)

    brief = code_agent.brief("change add behavior", root=root, budget=1200)
    assert brief["used"] <= brief["budget"]
    assert brief["sections"]
    assert "Code Brief" in code_agent.render_brief(brief)


def test_quality_reports_large_and_long_code(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    long_body = "\n".join("    total += 1" for _ in range(85))
    (root / "pkg" / "large.py").write_text(
        "def long_function():\n"
        "    total = 0\n"
        f"{long_body}\n"
        "    return total\n",
        encoding="utf-8",
    )

    data = code_agent.quality(root=root)
    assert data["findings"]
    assert any("long_function" in f.get("detail", "") for f in data["findings"])
    assert "Code Quality" in code_agent.render_quality(data)


def test_diff_brief_and_release_check(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    (root / "pkg" / "calc.py").write_text(
        "def add(left, right):\n"
        "    return left - right\n",
        encoding="utf-8",
    )

    brief = code_agent.diff_brief(root=root)
    assert brief["root"] == str(root)
    assert "## Summary" in brief["pr_body"]
    assert "Code Diff Brief" in code_agent.render_diff_brief(brief)

    check = code_agent.release_check(root=root, version="v0.1.0")
    assert check["version"] == "v0.1.0"
    assert "suggested_tests" in check
    assert "Code Release Check" in code_agent.render_release_check(check)


def test_refs_and_rename_plan(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    (root / "pkg" / "service.py").write_text("from pkg.calc import add\n\ndef total():\n    return add(1, 2)\n", encoding="utf-8")

    refs = code_agent.refs("add", root=root)
    assert any(item["kind"] == "definition" and item["path"] == "pkg/calc.py" for item in refs["matches"])
    assert any(item["path"] == "pkg/service.py" for item in refs["matches"])
    assert "Code Refs" in code_agent.render_refs(refs)

    plan = code_agent.rename_plan("add", "sum_values", root=root)
    assert plan["spec"]["ops"]
    assert plan["validation"]["ok"]
    paths = {op["path"] for op in plan["spec"]["ops"]}
    assert "pkg/calc.py" in paths
    assert "pkg/service.py" in paths
    assert "Code Rename Plan" in code_agent.render_rename_plan(plan)


def test_parse_pytest_output_and_repair_plan(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    output = """
_______________________________ test_add ________________________________
tests/test_calc.py:4: in test_add
    assert add(1, 2) == 4
pkg/calc.py:2: in add
    raise AssertionError("expected sum")
>   assert add(1, 2) == 4
E   AssertionError: expected sum
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - AssertionError: expected sum
"""
    parsed = code_agent.parse_pytest_output(output)
    assert parsed["failure_count"] == 1
    assert parsed["failures"][0]["path"] == "tests/test_calc.py"
    assert parsed["failures"][0]["exception_type"] == "AssertionError"
    assert parsed["failures"][0]["frames"][0]["path"] == "tests/test_calc.py"
    assert parsed["failures"][0]["frames"][1]["path"] == "pkg/calc.py"

    repair = code_agent.repair_plan(output, root=root)
    assert repair["failure_count"] == 1
    assert "tests/test_calc.py" in repair["likely_files"]
    assert "pkg/calc.py" in repair["likely_files"]
    assert repair["failure_summary"][0]["kind"] == "assertion"
    assert repair["failure_summary"][0]["rerun"] == "python -m pytest tests/test_calc.py::test_add"
    assert repair["focused_tests"] == ["python -m pytest tests/test_calc.py::test_add"]
    assert repair["repair_loop"]
    assert "Code Repair Plan" in code_agent.render_repair(repair)
    assert "exception: AssertionError" in code_agent.render_repair(repair)
    assert "Failure Summary" in code_agent.render_repair(repair)
    assert "Focused Tests" in code_agent.render_repair(repair)


def test_patch_apply_loop_dry_run_and_execute(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    spec = {
        "ops": [{
            "path": "pkg/calc.py",
            "old": "    return left + right\n",
            "new": "    return left + right + 0\n",
        }]
    }

    dry = code_agent.patch_apply_loop(spec, root=root, test_command="python -m pytest tests/test_calc.py", execute=False)
    assert dry["mode"] == "dry-run"
    assert dry["patch"]["status"] == "valid"
    assert dry["patch"]["apply"]["applied"] is False
    assert dry["test_result"] is None
    assert (root / "pkg" / "calc.py").read_text(encoding="utf-8").endswith("return left + right\n")

    run = code_agent.patch_apply_loop(spec, root=root, test_command="python -m pytest tests/test_calc.py", execute=True)
    assert run["mode"] == "execute"
    assert run["patch"]["status"] == "applied"
    assert run["test_result"]["ok"] is True
    assert "return left + right + 0" in (root / "pkg" / "calc.py").read_text(encoding="utf-8")
    assert "Code Run" in code_agent.render_run(run)


def test_repair_plan_classifies_import_and_syntax_failures(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    output = """
________________________ ERROR collecting tests/test_calc.py ________________________
E   ModuleNotFoundError: No module named 'pkg.missing'
=========================== short test summary info ============================
ERROR tests/test_calc.py - ModuleNotFoundError: No module named 'pkg.missing'
FAILED tests/test_calc.py::test_syntax - SyntaxError: invalid syntax
"""
    repair = code_agent.repair_plan(output, root=root)
    kinds = {item["nodeid"]: item["kind"] for item in repair["failure_summary"]}
    assert kinds["tests/test_calc.py"] == "import"
    assert kinds["tests/test_calc.py::test_syntax"] == "syntax"
    assert "python -m pytest tests/test_calc.py::test_syntax" in repair["focused_tests"]


def test_code_impact_maps_symbol_to_callers_and_tests(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    (root / "pkg" / "service.py").write_text("from pkg.calc import add\n\ndef total():\n    return add(1, 2)\n", encoding="utf-8")

    data = code_agent.impact("add", root=root)
    assert any(d["path"] == "pkg/calc.py" for d in data["definitions"])
    assert any(c["path"] == "pkg/service.py" for c in data["callers"])
    assert "tests/test_calc.py" in data["tests"]
    out = code_agent.render_impact(data)
    assert "Workspace Impact" in out
    assert "Next Steps" in out


def test_code_run_loop_dry_run(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    monkeypatch.setattr(code_agent, "CODE_RUN_DIR", tmp_path / ".ivyea" / "code-runs")

    data = code_agent.run_loop("change add behavior", root=root, persist=True, max_rounds=3)
    assert data["mode"] == "dry-run"
    assert data["patch"]["status"] == "needs_input"
    assert "pkg/calc.py" in data["plan"]["relevant_files"]
    assert data["selected_tests"]
    assert data["test_result"] is None
    assert len(data["rounds"]) == 3
    assert data["rounds"][1]["status"] == "blocked_waiting_for_previous_round"
    out = code_agent.render_run(data)
    assert "Code Run" in out
    assert "Patch" in out
    assert "Review Gate" in out
    assert "Rounds" in out
    loaded = code_agent.load_run(data["id"])
    assert loaded["id"] == data["id"]
    rows = code_agent.list_runs()
    assert rows and rows[0]["id"] == data["id"]
    assert "Code Runs" in code_agent.render_run_list(rows)


def test_code_task_bundle_packages_multiround_context(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    output = "FAILED tests/test_calc.py::test_add - AssertionError: expected sum"

    data = code_agent.task_bundle("change add behavior", root=root, test_output=output)

    assert data["mode"] == "read-only-task-bundle"
    assert "pkg/calc.py" in data["plan"]["relevant_files"]
    assert any(item["name"] == "repair" and item["status"] == "ready" for item in data["phases"])
    assert data["repair"]["failure_summary"][0]["kind"] == "assertion"
    assert "继续 Ivyea 代码任务" in data["resume_prompt"]
    rendered = code_agent.render_bundle(data)
    assert "Code Task Bundle" in rendered
    assert "Resume Prompt" in rendered


def test_patch_candidate_template_and_validation(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)

    draft = code_agent.patch_candidate("change add behavior", root=root)
    assert draft["status"] == "needs_input"
    assert draft["spec"]["ops"][0]["path"] == "pkg/calc.py"
    assert "Code Patch Candidate" in code_agent.render_patch_candidate(draft)

    valid = code_agent.patch_candidate(
        "change add behavior",
        root=root,
        path="pkg/calc.py",
        old="def add(left, right):\n    return left + right\n",
        new="def add(left, right):\n    return left - right\n",
    )
    assert valid["status"] == "valid"
    assert valid["validation"]["ok"]


def test_llm_patch_candidate_request_and_validation(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    req = code_agent.llm_patch_candidate("change add behavior", root=root)
    assert req["status"] == "request_only"
    assert "LLM Request" in code_agent.render_patch_candidate(req)

    class FakeProvider:
        def complete(self, system, user, **kwargs):
            assert "Return only JSON" in system
            return (
                '{"ops":[{"path":"pkg/calc.py",'
                '"old":"def add(left, right):\\n    return left + right\\n",'
                '"new":"def add(left, right):\\n    return left - right\\n"}]}'
            )

    result = code_agent.llm_patch_candidate("change add behavior", root=root, provider=FakeProvider())
    assert result["status"] == "valid"
    assert result["validation"]["ok"]


def test_code_run_can_include_llm_patch_request(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    data = code_agent.run_loop("change add behavior", root=root, llm_patch=True)
    assert data["patch"]["status"] == "request_only"
    assert data["patch"]["request"]["system"]
    assert "LLM Request" in code_agent.render_patch_candidate(data["patch"])


def test_sandbox_plan_is_dry_run(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    data = code_agent.sandbox_plan(root=root, name="demo")
    assert data["method"] == "git-worktree"
    assert data["execute"] is False
    assert "git worktree add" in data["commands"][0]
    assert "Code Sandbox Plan" in code_agent.render_sandbox_plan(data)


def test_review_ready_and_cli_smoke(tmp_path, monkeypatch):
    root = _make_project(tmp_path, monkeypatch)
    result = code_agent.review_ready(root=root)
    assert result["ok"]
    assert result["suggested_tests"]
    assert "Code Review Gate" in code_agent.render_review(result)

    proc = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "plan", "change add behavior", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0
    assert "Code Plan" in proc.stdout
    assert "pkg/calc.py" in proc.stdout

    brief = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "brief", "change add behavior", "--root", str(root), "--budget", "1200"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert brief.returncode == 0
    assert "Code Brief" in brief.stdout

    quality = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "quality", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert quality.returncode == 0
    assert "Code Quality" in quality.stdout

    bundle = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "bundle", "change add behavior", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert bundle.returncode == 0
    assert "Code Task Bundle" in bundle.stdout

    refs = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "refs", "add", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert refs.returncode == 0
    assert "Code Refs" in refs.stdout

    rename = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "rename-plan", "add", "--new-name", "sum_values", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert rename.returncode == 0
    assert "Code Rename Plan" in rename.stdout

    diff_brief = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "diff-brief", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert diff_brief.returncode == 0
    assert "Code Diff Brief" in diff_brief.stdout

    release = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "release-check", "--root", str(root), "--version", "v0.1.0"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert release.returncode == 0
    assert "Code Release Check" in release.stdout

    impact = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "impact", "add", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert impact.returncode == 0
    assert "Workspace Impact" in impact.stdout

    run = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "run", "change add behavior", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert run.returncode == 0
    assert "Code Run" in run.stdout
    assert "needs_input" in run.stdout

    runs = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "runs", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert runs.returncode == 0
    assert "Code Runs" in runs.stdout

    patch = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "patch", "change add behavior", "--root", str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert patch.returncode == 0
    assert "Code Patch Candidate" in patch.stdout

    llm_patch = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "patch", "change add behavior", "--root", str(root), "--llm"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert llm_patch.returncode == 0
    assert "LLM Request" in llm_patch.stdout

    run_llm = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "run", "change add behavior", "--root", str(root), "--llm-patch"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert run_llm.returncode == 0
    assert "request_only" in run_llm.stdout

    sandbox = subprocess.run(
        [sys.executable, "-m", "ivyea_agent", "code", "sandbox", "--root", str(root), "--name", "demo"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "IVYEA_HOME": str(root / ".ivyea-cli")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert sandbox.returncode == 0
    assert "Code Sandbox Plan" in sandbox.stdout
