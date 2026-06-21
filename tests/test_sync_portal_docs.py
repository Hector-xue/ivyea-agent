from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sync_portal_docs.py"
    spec = importlib.util.spec_from_file_location("sync_portal_docs", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_replace_versions_and_extract_commands():
    mod = _load_script()
    assert mod.replace_versions("use v0.5.2 and v1.2.3", "9.8.7") == "use v9.8.7 and v9.8.7"
    commands = mod.extract_commands("```bash\nivyea self status\n# skip\ncurl http://x\n```\n")
    assert commands == ["ivyea self status", "curl http://x"]


def test_sync_site_writes_data(tmp_path):
    mod = _load_script()
    (tmp_path / "index.html").write_text("<span>v0.1.0</span>", encoding="utf-8")
    changed = mod.sync_site(tmp_path)
    version = mod.project_version()
    assert tmp_path / "portal-data.json" in changed
    assert f"v{version}" in (tmp_path / "index.html").read_text(encoding="utf-8")
    data = json.loads((tmp_path / "portal-data.json").read_text(encoding="utf-8"))
    assert data["version"] == f"v{version}"
    assert "Workspace 项目理解" in data["capabilities"]
