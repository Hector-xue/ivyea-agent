"""Exec sandbox resource limits (POSIX rlimit)."""
from __future__ import annotations

import os

import pytest

from ivyea_agent import tools_general as tg
from ivyea_agent.agent_tools import ToolContext, dispatch


def test_make_preexec_none_on_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert tg._make_preexec(10) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit only")
def test_make_preexec_callable_on_posix():
    assert callable(tg._make_preexec(10))


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit only")
def test_run_python_child_gets_memory_limit(monkeypatch):
    monkeypatch.setattr(tg.permission, "request_intent", lambda *a, **k: tg.permission.APPROVE)
    monkeypatch.setattr(tg.config, "get_setting",
                        lambda k, d=None: {"exec_memory_limit_mb": 1024, "exec_file_limit_mb": 0}.get(k, d))
    ctx = ToolContext(workspace="/tmp")
    out = dispatch("run_python",
                   {"code": "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"}, ctx)
    assert str(1024 * 1024 * 1024) in out   # the child actually runs under the 1 GiB cap


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit only")
def test_run_python_still_works_normally(monkeypatch):
    monkeypatch.setattr(tg.permission, "request_intent", lambda *a, **k: tg.permission.APPROVE)
    ctx = ToolContext(workspace="/tmp")
    out = dispatch("run_python", {"code": "print(6 * 7)"}, ctx)
    assert "42" in out
