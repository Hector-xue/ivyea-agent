"""Self-management helpers for install lifecycle tasks."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from . import __version__, config, policy


def install_info() -> dict[str, Any]:
    exe = Path(sys.executable).resolve()
    prefix = Path(sys.prefix).resolve()
    ivyea_dir = config.IVYEA_DIR.expanduser().resolve()
    method = "unknown"
    if "pipx" in str(prefix).lower() or os.environ.get("PIPX_HOME"):
        method = "pipx"
    elif str(prefix).startswith(str(ivyea_dir / "runtime")):
        method = "ivyea-runtime"
    elif (prefix / "pyvenv.cfg").exists():
        method = "venv"
    elif hasattr(sys, "base_prefix") and sys.prefix != sys.base_prefix:
        method = "venv"
    return {
        "version": __version__,
        "python": str(exe),
        "prefix": str(prefix),
        "method": method,
        "ivyea_dir": str(ivyea_dir),
        "ivyea_bin": shutil.which("ivyea") or "",
        "pipx": shutil.which("pipx") or "",
        "platform": sys.platform,
    }


def upgrade_plan(version: str = "latest", ref: str = "", method: str = "") -> dict[str, Any]:
    info = install_info()
    chosen = method or info["method"]
    commands: list[str] = []
    if chosen == "pipx":
        if version and version != "latest":
            commands.append(f"IVYEA_VERSION={version} curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash")
        elif ref:
            commands.append(f"IVYEA_REF={ref} curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash")
        else:
            commands.append("pipx upgrade ivyea-agent")
    elif chosen == "ivyea-runtime":
        commands.append("curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash")
    else:
        commands.append("python -m pip install --upgrade ivyea-agent")
    return {"action": "upgrade", "info": info, "method": chosen, "commands": commands}


def uninstall_plan(keep_data: bool = True, method: str = "") -> dict[str, Any]:
    info = install_info()
    chosen = method or info["method"]
    commands: list[str] = []
    if chosen == "pipx":
        commands.append("pipx uninstall ivyea-agent")
    elif chosen == "ivyea-runtime":
        commands.append("python -m pip uninstall -y ivyea-agent")
    else:
        commands.append("python -m pip uninstall -y ivyea-agent")
    manual_steps = []
    if chosen == "ivyea-runtime":
        manual_steps.append(f"如需完全删除 runtime，请手动删除：{Path(info['ivyea_dir']) / 'runtime'}")
        manual_steps.append(f"如需删除启动器，请手动删除：{Path.home() / '.local' / 'bin' / 'ivyea'}")
    if not keep_data:
        manual_steps.append(f"用户数据不会自动删除；确认备份后可手动删除：{info['ivyea_dir']}")
    return {
        "action": "uninstall",
        "info": info,
        "method": chosen,
        "keep_data": keep_data,
        "commands": commands,
        "manual_steps": manual_steps,
    }


def execute_plan(plan: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for command in plan.get("commands") or []:
        assessed = policy.assess_command(command)
        if not assessed.get("ok"):
            results.append({"command": command, "ok": False, "output": "\n".join(assessed.get("reasons") or [])})
            break
        proc = subprocess.run(
            ["bash", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        results.append({"command": command, "ok": proc.returncode == 0, "returncode": proc.returncode, "output": proc.stdout.strip()})
        if proc.returncode != 0:
            break
    return {"ok": all(r.get("ok") for r in results), "plan": plan, "results": results}


def backup(path: str | Path | None = None) -> Path:
    src = config.IVYEA_DIR.expanduser().resolve()
    out = Path(path).expanduser() if path else src / "backups" / f"ivyea-backup-{__version__}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if not src.exists():
            return out
        for item in src.rglob("*"):
            if item == out or "backups" in item.relative_to(src).parts:
                continue
            if item.is_file():
                zf.write(item, item.relative_to(src).as_posix())
    return out


def render_status(info: dict[str, Any] | None = None) -> str:
    info = info or install_info()
    return "\n".join([
        "Ivyea Self Status",
        "",
        f"- version: {info.get('version')}",
        f"- method: {info.get('method')}",
        f"- python: {info.get('python')}",
        f"- prefix: {info.get('prefix')}",
        f"- ivyea_bin: {info.get('ivyea_bin') or '-'}",
        f"- ivyea_dir: {info.get('ivyea_dir')}",
        f"- pipx: {info.get('pipx') or '-'}",
    ])


def render_plan(plan: dict[str, Any]) -> str:
    lines = [
        "Ivyea Self Plan",
        "",
        f"- action: {plan.get('action')}",
        f"- method: {plan.get('method')}",
    ]
    if "keep_data" in plan:
        lines.append(f"- keep_data: {plan.get('keep_data')}")
    lines.append("")
    lines.append("Commands:")
    lines.extend(f"- {cmd}" for cmd in plan.get("commands") or [])
    manual = plan.get("manual_steps") or []
    if manual:
        lines.append("")
        lines.append("Manual steps:")
        lines.extend(f"- {step}" for step in manual)
    return "\n".join(lines)


def render_execution(result: dict[str, Any]) -> str:
    lines = ["Ivyea Self Execution", "", f"- ok: {result.get('ok')}"]
    for row in result.get("results") or []:
        lines.append("")
        lines.append(f"$ {row.get('command')}")
        lines.append(f"ok={row.get('ok')} returncode={row.get('returncode', '-')}")
        if row.get("output"):
            lines.append(str(row["output"])[:2000])
    return "\n".join(lines)
