from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_offline_bundle.py"
    spec = importlib.util.spec_from_file_location("build_offline_bundle", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_offline_bundle_selects_current_version_wheel(tmp_path):
    mod = _load_script()
    (tmp_path / "ivyea_agent-0.5.5-py3-none-any.whl").touch()
    current = tmp_path / "ivyea_agent-1.0.18-py3-none-any.whl"
    current.touch()

    assert mod.project_wheel_path(tmp_path, "1.0.18") == current


def test_offline_bundle_requires_current_version_wheel(tmp_path):
    mod = _load_script()
    (tmp_path / "ivyea_agent-0.5.5-py3-none-any.whl").touch()

    try:
        mod.project_wheel_path(tmp_path, "1.0.18")
    except SystemExit as exc:
        assert "1.0.18" in str(exc)
    else:
        raise AssertionError("expected SystemExit for missing current wheel")


def test_offline_bundle_copies_semantic_model(tmp_path):
    mod = _load_script()
    model = tmp_path / "bge-model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    manifest = mod.copy_semantic_model(bundle, model, "BAAI/bge-small-zh-v1.5")

    assert manifest["backend"] == "sentence-transformers"
    assert manifest["model"] == "BAAI/bge-small-zh-v1.5"
    assert manifest["name"] == "bge-small-zh-v1.5"
    copied = bundle / manifest["model_dir"] / "config.json"
    assert copied.exists()
    assert (bundle / "semantic-manifest.json").exists()
