"""read_file line-range support (offset/limit) — the approval-loop root fix."""
from __future__ import annotations

from ivyea_agent.agent_tools import TOOL_SCHEMAS, ToolContext, dispatch


def _file(tmp_path, n=20):
    p = tmp_path / "big.txt"
    p.write_text("\n".join(f"line{i}" for i in range(1, n + 1)), encoding="utf-8")
    return p


def test_whole_file_when_no_range(tmp_path):
    out = dispatch("read_file", {"path": str(_file(tmp_path))}, ToolContext())
    assert "line1" in out and "line20" in out


def test_offset_and_limit_slice(tmp_path):
    out = dispatch("read_file", {"path": str(_file(tmp_path)), "offset": 5, "limit": 3}, ToolContext())
    assert "第 5–7 行" in out
    assert "line5" in out and "line7" in out
    assert "line4" not in out and "line8" not in out


def test_offset_only_reads_to_end(tmp_path):
    out = dispatch("read_file", {"path": str(_file(tmp_path)), "offset": 19}, ToolContext())
    assert "line19" in out and "line20" in out and "line18" not in out


def test_offset_out_of_range(tmp_path):
    out = dispatch("read_file", {"path": str(_file(tmp_path)), "offset": 99}, ToolContext())
    assert "超出范围" in out


def test_schema_exposes_offset_limit():
    rf = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "read_file")
    props = rf["function"]["parameters"]["properties"]
    assert "offset" in props and "limit" in props
