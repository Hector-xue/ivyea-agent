"""Chat-exposed code navigation tools (grep / code_search / code_symbols / code_impact)."""
from __future__ import annotations

from ivyea_agent.agent_tools import TOOL_SCHEMAS, ToolContext, dispatch


def _make_repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "use.py").write_text(
        "from .calc import add\n\n\ndef total(xs):\n    return add(sum(xs), 0)\n",
        encoding="utf-8",
    )
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01add\x00binary")
    return ToolContext(workspace=str(tmp_path))


def test_grep_finds_content_and_skips_binary(tmp_path):
    ctx = _make_repo(tmp_path)
    out = dispatch("grep", {"pattern": r"def add", "glob": "*.py"}, ctx)
    assert "calc.py" in out and "def add" in out
    # binary file with the literal bytes "add" must not appear
    assert "blob.bin" not in out


def test_grep_bad_regex_and_empty(tmp_path):
    ctx = _make_repo(tmp_path)
    assert "pattern 为空" in dispatch("grep", {}, ctx)
    assert "正则无效" in dispatch("grep", {"pattern": "("}, ctx)


def test_code_search_and_symbols(tmp_path):
    ctx = _make_repo(tmp_path)
    assert "calc.py" in dispatch("code_search", {"query": "add"}, ctx)
    assert "add" in dispatch("code_symbols", {"query": "add"}, ctx)


def test_code_impact_reports_callers(tmp_path):
    ctx = _make_repo(tmp_path)
    out = dispatch("code_impact", {"target": "add"}, ctx)
    assert "add" in out


def test_tools_registered_in_schema():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert {"grep", "code_search", "code_symbols", "code_impact"} <= names
