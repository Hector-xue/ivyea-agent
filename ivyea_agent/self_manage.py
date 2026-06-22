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


def install_doctor(info: dict[str, Any] | None = None) -> dict[str, Any]:
    info = info or install_info()
    checks: list[dict[str, Any]] = []

    version_ok = sys.version_info >= (3, 9)
    checks.append({
        "name": "python",
        "status": "ok" if version_ok else "fail",
        "detail": f"{sys.version.split()[0]} at {info.get('python')}",
        "fix": "安装 Python 3.9+；Windows 安装时勾选 Add Python to PATH。" if not version_ok else "",
    })

    ivyea_bin = info.get("ivyea_bin") or ""
    checks.append({
        "name": "ivyea command",
        "status": "ok" if ivyea_bin else "warn",
        "detail": ivyea_bin or "当前 shell 找不到 ivyea",
        "fix": _path_fix(info) if not ivyea_bin else "",
    })

    ivyea_dir = Path(str(info.get("ivyea_dir") or config.IVYEA_DIR)).expanduser()
    try:
        ivyea_dir.mkdir(parents=True, exist_ok=True)
        probe = ivyea_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        data_status, data_detail, data_fix = "ok", str(ivyea_dir), ""
    except OSError as exc:
        data_status, data_detail, data_fix = "fail", f"{ivyea_dir}: {exc}", "检查目录权限，或设置 IVYEA_HOME 到可写目录。"
    checks.append({"name": "data dir", "status": data_status, "detail": data_detail, "fix": data_fix})

    checks.append({
        "name": "install method",
        "status": "ok" if info.get("method") != "unknown" else "warn",
        "detail": str(info.get("method") or "unknown"),
        "fix": "建议使用一键安装脚本或 pipx，便于升级和卸载。" if info.get("method") == "unknown" else "",
    })

    optional = []
    for module, label, fix in [
        ("pandas", "pandas reports", "需要表格分析时安装：python -m pip install pandas openpyxl"),
        ("PIL", "image audit", "需要图片尺寸/格式分析时安装：python -m pip install pillow"),
    ]:
        try:
            __import__(module)
            optional.append({"name": label, "status": "ok", "detail": module, "fix": ""})
        except ImportError:
            optional.append({"name": label, "status": "warn", "detail": f"{module} not installed", "fix": fix})
    checks.extend(optional)

    from . import retrieval_embeddings
    emb = retrieval_embeddings.status()
    if emb["configured_backend"] == "hash":
        checks.append({
            "name": "retrieval embeddings",
            "status": "ok",
            "detail": f"default {emb['active_backend']} ({emb['vector_kind']})",
            "fix": "",
        })
    elif emb["semantic_enabled"]:
        checks.append({
            "name": "retrieval embeddings",
            "status": "ok",
            "detail": f"{emb['active_backend']} {emb['model']} ({emb['vector_kind']})",
            "fix": "",
        })
    else:
        checks.append({
            "name": "retrieval embeddings",
            "status": "warn",
            "detail": f"fallback to {emb['active_backend']}: {emb.get('fallback_reason') or 'not ready'}",
            "fix": emb.get("install_hint") or "运行 `ivyea retrieval embeddings --backend hash` 使用默认本地索引。",
        })

    ok = all(c["status"] != "fail" for c in checks)
    return {"ok": ok, "info": info, "checks": checks, "next_steps": _doctor_next_steps(checks)}


def _path_fix(info: dict[str, Any]) -> str:
    if sys.platform.startswith("win"):
        candidate = str(Path.home() / ".ivyea" / "bin")
        return f"重开 PowerShell；仍不可用时把 {candidate} 加入用户 PATH。"
    candidate = str(Path.home() / ".local" / "bin")
    return f"重开终端，或先执行：export PATH=\"{candidate}:$PATH\""


def _doctor_next_steps(checks: list[dict[str, Any]]) -> list[str]:
    steps = []
    if any(c["status"] == "fail" for c in checks):
        steps.append("先处理 fail 项，再重新运行 `ivyea self doctor`。")
    if any(c["name"] == "ivyea command" and c["status"] != "ok" for c in checks):
        steps.append("修复 PATH 后重开终端，确认 `ivyea --help` 能执行。")
    steps.append("首次使用运行 `ivyea config` 选择模型并配置密钥。")
    steps.append("配置完成后运行 `ivyea doctor` 或 `ivyea chat` 验证。")
    return steps


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


def render_doctor(data: dict[str, Any]) -> str:
    lines = [
        "Ivyea Install Doctor",
        "",
        f"- ok: {data.get('ok')}",
        f"- platform: {(data.get('info') or {}).get('platform')}",
        f"- method: {(data.get('info') or {}).get('method')}",
    ]
    lines.extend(["", "Checks"])
    for check in data.get("checks") or []:
        fix = f" | fix: {check.get('fix')}" if check.get("fix") else ""
        lines.append(f"- {check.get('status')} {check.get('name')}: {check.get('detail')}{fix}")
    steps = data.get("next_steps") or []
    if steps:
        lines.extend(["", "Next Steps"])
        lines.extend(f"{i}. {step}" for i, step in enumerate(steps, start=1))
    return "\n".join(lines)


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
