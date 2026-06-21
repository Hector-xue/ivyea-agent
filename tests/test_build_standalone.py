from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_standalone.py"
    spec = importlib.util.spec_from_file_location("build_standalone", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_standalone_script_helpers():
    mod = _load_script()
    version = mod.project_version()
    assert version
    name = mod.exe_name(version)
    assert name.startswith(f"ivyea-agent-{version}-")


def test_standalone_missing_pyinstaller(monkeypatch, capsys):
    mod = _load_script()
    monkeypatch.setattr(mod, "pyinstaller_available", lambda _python: False)
    assert mod.main([]) == 2
    err = capsys.readouterr().err
    assert "PyInstaller is not installed" in err
