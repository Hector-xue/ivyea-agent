#!/usr/bin/env python3
"""Build a self-contained Ivyea Agent offline installer bundle."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def project_version() -> str:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def archive_dir(bundle_dir: Path, out_dir: Path) -> None:
    tar_path = out_dir / f"{bundle_dir.name}.tar.gz"
    zip_path = out_dir / f"{bundle_dir.name}.zip"
    if tar_path.exists():
        tar_path.unlink()
    if zip_path.exists():
        zip_path.unlink()

    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(bundle_dir, arcname=bundle_dir.name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in bundle_dir.rglob("*"):
            zf.write(path, path.relative_to(bundle_dir.parent))


def project_wheel_path(dist_dir: Path, version: str) -> Path:
    wheels = sorted(dist_dir.glob(f"ivyea_agent-{version}-*.whl"))
    if not wheels:
        raise SystemExit(f"No ivyea_agent {version} wheel produced in dist/")
    return wheels[-1]


def safe_model_name(value: str) -> str:
    name = (value or "").strip().replace("\\", "/").rstrip("/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip(".-")
    return name or "embedding-model"


def copy_semantic_model(bundle_dir: Path, model_dir: Path, model_name: str = "") -> dict[str, str]:
    src = model_dir.expanduser().resolve()
    if not src.is_dir():
        raise SystemExit(f"semantic model dir does not exist: {src}")
    name = safe_model_name(model_name or src.name)
    rel = Path("models") / "embedding" / name
    dst = bundle_dir / rel
    copy_tree(src, dst)
    manifest = {
        "backend": "sentence-transformers",
        "model": model_name or name,
        "name": name,
        "model_dir": rel.as_posix(),
    }
    (bundle_dir / "semantic-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="dist/offline", help="Output directory")
    parser.add_argument("--python", default=sys.executable, help="Python executable used for build/download")
    parser.add_argument("--with-semantic", action="store_true", help="Include optional sentence-transformers wheels")
    parser.add_argument("--semantic-model-dir", help="Pre-bundled local sentence-transformers model directory")
    parser.add_argument("--semantic-model-name", default="", help="Model name written into Ivyea settings")
    parser.add_argument("--no-archive", action="store_true", help="Only create directory, skip zip/tar.gz")
    args = parser.parse_args()

    version = project_version()
    dist_dir = ROOT / "dist"
    out_dir = ROOT / args.output
    wheelhouse = out_dir / f"ivyea-agent-offline-{version}" / "wheelhouse"
    bundle_dir = wheelhouse.parent

    out_dir.mkdir(parents=True, exist_ok=True)
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    wheelhouse.mkdir(parents=True)

    run([args.python, "-m", "pip", "install", "--upgrade", "build"])
    run([args.python, "-m", "build", "--wheel"])

    project_wheel = project_wheel_path(dist_dir, version)

    run([args.python, "-m", "pip", "download", str(project_wheel), "-d", str(wheelhouse)])
    include_semantic = bool(args.with_semantic or args.semantic_model_dir)
    semantic_manifest = None
    if include_semantic:
        run([args.python, "-m", "pip", "download", "sentence-transformers>=3.0", "-d", str(wheelhouse)])
        (wheelhouse / ".ivyea-semantic").write_text("sentence-transformers\n", encoding="utf-8")
    if args.semantic_model_dir:
        semantic_manifest = copy_semantic_model(bundle_dir, Path(args.semantic_model_dir), args.semantic_model_name)

    shutil.copy2(ROOT / "scripts" / "install.sh", bundle_dir / "install.sh")
    shutil.copy2(ROOT / "scripts" / "install.ps1", bundle_dir / "install.ps1")
    (bundle_dir / "README.txt").write_text(
        "\n".join(
            [
                f"Ivyea Agent offline installer v{version}",
                "",
                "Linux/macOS:",
                "  bash install.sh",
                "",
                "Windows PowerShell:",
                "  powershell -ExecutionPolicy Bypass -File .\\install.ps1",
                "",
                "The installer uses ./wheelhouse and does not download Python packages.",
                "If this bundle was built with --with-semantic, it also installs the local semantic retrieval dependency.",
                "If semantic-manifest.json is present, the installer copies the bundled embedding model into ~/.ivyea/models/embedding,",
                "configures retrieval_embedding_model_path, and runs ivyea retrieval sync after install.",
                "If Python 3.9+ is not installed, install Python first or allow the online installer to bootstrap it.",
                f"Bundled semantic model: {semantic_manifest['model']} ({semantic_manifest['model_dir']})" if semantic_manifest else "",
                "",
            ]
        ),
        encoding="utf-8",
    )

    if not args.no_archive:
        archive_dir(bundle_dir, out_dir)

    print(f"\nOffline bundle: {bundle_dir}")
    if not args.no_archive:
        print(f"Archives: {out_dir / (bundle_dir.name + '.tar.gz')}")
        print(f"          {out_dir / (bundle_dir.name + '.zip')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
