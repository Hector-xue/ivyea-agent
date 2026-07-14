#!/usr/bin/env python3
"""Build a single-file Ivyea Agent executable with PyInstaller."""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    # Single source of truth: ivyea_agent/__init__.py.__version__ (pyproject.toml
    # declares version dynamically from this same attr, so it's no longer static).
    for line in (ROOT / "ivyea_agent" / "__init__.py").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("__version__"):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("could not read __version__ from ivyea_agent/__init__.py")


def exe_name(version: str) -> str:
    system = platform.system().lower() or "unknown"
    machine = platform.machine().lower() or "unknown"
    suffix = ".exe" if system == "windows" else ""
    return f"ivyea-agent-{version}-{system}-{machine}{suffix}"


def pyinstaller_available(python: str) -> bool:
    proc = subprocess.run(
        [python, "-m", "PyInstaller", "--version"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output", default="dist/standalone")
    parser.add_argument("--name", help="Override executable filename")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--no-onefile", action="store_true", help="Build a directory instead of one executable")
    args = parser.parse_args(argv)

    version = project_version()
    out_dir = ROOT / args.output
    out_dir.mkdir(parents=True, exist_ok=True)
    name = args.name or exe_name(version)

    if not pyinstaller_available(args.python):
        print("PyInstaller is not installed for this Python.", file=sys.stderr)
        print(f"Install it with: {args.python} -m pip install pyinstaller", file=sys.stderr)
        return 2

    build_dir = ROOT / "build" / "standalone"
    spec_dir = ROOT / "build" / "spec"
    if args.clean:
        shutil.rmtree(build_dir, ignore_errors=True)
        shutil.rmtree(spec_dir, ignore_errors=True)
    spec_dir.mkdir(parents=True, exist_ok=True)
    entry = spec_dir / "ivyea_entry.py"
    entry.write_text(
        "from ivyea_agent.cli import main\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )

    cmd = [
        args.python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        name,
        "--distpath",
        str(out_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(spec_dir),
        "--collect-data",
        "ivyea_agent",
        str(entry),
    ]
    if not args.no_onefile:
        cmd.insert(3, "--onefile")

    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)
    print(f"\nStandalone output: {out_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
