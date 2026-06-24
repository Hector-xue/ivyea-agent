"""Self-management helpers for install lifecycle tasks."""
from __future__ import annotations

import os
import json
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

from . import __version__, config, policy


def _run_dir() -> Path:
    return config.IVYEA_DIR / "run"


def _log_dir() -> Path:
    return config.IVYEA_DIR / "logs"


def _pid_file() -> Path:
    return _run_dir() / "ivyea-agent.pid"


def _service_log_file() -> Path:
    return _log_dir() / "ivyea-agent.log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _service_command(host: str = "127.0.0.1", port: int = 8765, allow_remote: bool = False) -> list[str]:
    cmd = [sys.executable, "-m", "ivyea_agent.cli", "serve", "--host", host, "--port", str(int(port))]
    if allow_remote:
        cmd.append("--allow-remote")
    return cmd


def _is_local_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return str(pid) in proc.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _pid_cmdline(pid: int) -> list[str]:
    if pid <= 0 or os.name == "nt":
        return []
    path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _pid_matches_service(pid: int, port: int | None = None) -> bool:
    parts = _pid_cmdline(pid)
    if not parts:
        proc_cmdline = Path("/proc") / str(pid) / "cmdline"
        if os.name != "nt" and Path("/proc").exists() and proc_cmdline.exists():
            return False
        return True
    names = {Path(part).name.lower() for part in parts[:2]}
    joined = " ".join(parts)
    is_ivyea = "ivyea_agent.cli" in joined or "ivyea_agent" in joined or bool(names & {"ivyea", "ivyea.exe"})
    if not is_ivyea or "serve" not in parts:
        return False
    if port is None:
        return True
    wanted = str(int(port))
    for idx, part in enumerate(parts):
        if part == "--port" and idx + 1 < len(parts):
            return parts[idx + 1] == wanted
        if part.startswith("--port="):
            return part.split("=", 1)[1] == wanted
    return True


def _read_pid_meta() -> dict[str, Any]:
    path = _pid_file()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        try:
            return {"pid": int(raw)}
        except ValueError:
            return {}


def _write_pid_meta(meta: dict[str, Any]) -> None:
    _run_dir().mkdir(parents=True, exist_ok=True)
    _pid_file().write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _probe_health(host: str = "127.0.0.1", port: int = 8765, timeout: float = 1.5) -> dict[str, Any]:
    url = f"http://{host}:{int(port)}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {"ok": False, "error": "health returned non-object JSON"}
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def service_log_tail(lines: int = 80) -> dict[str, Any]:
    path = _service_log_file()
    limit = max(1, min(int(lines or 80), 500))
    if not path.exists():
        return {"ok": True, "path": str(path), "lines": [], "text": ""}
    try:
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError as exc:
        return {"ok": False, "path": str(path), "lines": [], "text": "", "error": str(exc)}
    return {"ok": True, "path": str(path), "lines": rows, "text": "\n".join(rows)}


def service_status(host: str = "127.0.0.1", port: int = 8765, probe: bool = True) -> dict[str, Any]:
    meta = _read_pid_meta()
    pid = int(meta.get("pid") or 0)
    pid_process_running = _pid_running(pid) if pid else False
    pid_matches_service = _pid_matches_service(pid, int(port)) if pid_process_running else False
    pid_running = bool(pid_process_running and pid_matches_service)
    health = _probe_health(host, port) if probe else {"ok": False, "skipped": True}
    health_running = bool(health.get("ok"))
    running = bool(pid_running or health_running)
    stale_pid = bool(pid and (not pid_running or not pid_matches_service) and not health_running)
    return {
        "ok": True,
        "running": running,
        "pid": pid if pid else None,
        "pid_process_running": pid_process_running,
        "pid_matches_service": pid_matches_service,
        "pid_running": pid_running,
        "health_running": health_running,
        "stale_pid": stale_pid,
        "host": host,
        "port": int(port),
        "base_url": f"http://{host}:{int(port)}",
        "pid_file": str(_pid_file()),
        "log_file": str(_service_log_file()),
        "meta": meta,
        "health": health,
        "command": " ".join(_service_command(host, port, allow_remote=not _is_local_host(host))),
    }


def service_start(
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_remote: bool = False,
    api_token: str = "",
    wait: bool = True,
    timeout: float = 10.0,
) -> dict[str, Any]:
    host = host or "127.0.0.1"
    port = int(port or 8765)
    if not _is_local_host(host) and not allow_remote:
        return {"ok": False, "error": "remote_bind_requires_allow_remote", "detail": "非 localhost 监听必须设置 allow_remote=True"}
    token = api_token or os.environ.get("IVYEA_API_TOKEN", "")
    if not _is_local_host(host) and not token:
        return {"ok": False, "error": "remote_bind_requires_token", "detail": "非 localhost 监听必须配置 IVYEA_API_TOKEN"}

    current = service_status(host, port, probe=True)
    if current.get("running"):
        return {"ok": True, "already_running": True, "service": current}

    config.ensure_dirs()
    _run_dir().mkdir(parents=True, exist_ok=True)
    _log_dir().mkdir(parents=True, exist_ok=True)
    cmd = _service_command(host, port, allow_remote=allow_remote)
    env = os.environ.copy()
    if token:
        env["IVYEA_API_TOKEN"] = token
    log_path = _service_log_file()
    log_fh = log_path.open("ab")
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            close_fds=os.name != "nt",
            start_new_session=start_new_session,
            creationflags=creationflags,
        )
    finally:
        log_fh.close()

    meta = {
        "pid": proc.pid,
        "host": host,
        "port": port,
        "started_at": _now(),
        "python": sys.executable,
        "command": " ".join(cmd),
        "api_token_configured": bool(token),
    }
    _write_pid_meta(meta)

    if not wait:
        return {"ok": True, "started": True, "pid": proc.pid, "service": service_status(host, port, probe=False)}

    deadline = time.time() + max(1.0, float(timeout or 10.0))
    last_health: dict[str, Any] = {}
    while time.time() < deadline:
        if proc.poll() is not None:
            return {
                "ok": False,
                "started": False,
                "pid": proc.pid,
                "returncode": proc.returncode,
                "error": "process_exited",
                "logs": service_log_tail(80),
            }
        last_health = _probe_health(host, port, timeout=1.0)
        if last_health.get("ok"):
            return {"ok": True, "started": True, "pid": proc.pid, "service": service_status(host, port, probe=True)}
        time.sleep(0.25)
    return {
        "ok": False,
        "started": False,
        "pid": proc.pid,
        "error": "health_timeout",
        "health": last_health,
        "logs": service_log_tail(80),
    }


def service_stop(timeout: float = 10.0, force: bool = False) -> dict[str, Any]:
    current = service_status(probe=False)
    pid = int(current.get("pid") or 0)
    if not pid or not current.get("pid_running"):
        try:
            _pid_file().unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": True, "already_stopped": True, "service": current}

    if os.name == "nt":
        cmd = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            cmd.append("/F")
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        stopped = proc.returncode == 0
    else:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        deadline = time.time() + max(1.0, float(timeout or 10.0))
        stopped = False
        while time.time() < deadline:
            if not _pid_running(pid):
                stopped = True
                break
            time.sleep(0.25)
        if not stopped and force:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.25)
            stopped = not _pid_running(pid)
    if stopped:
        try:
            _pid_file().unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": stopped, "stopped": stopped, "pid": pid, "service": service_status(probe=False)}


def autostart_files(host: str = "127.0.0.1", port: int = 8765) -> dict[str, Any]:
    host = host or "127.0.0.1"
    port = int(port or 8765)
    cmd = _service_command(host, port, allow_remote=not _is_local_host(host))
    if sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / "com.ivyea.agent.plist"
        content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ivyea.agent</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
""".format(
            args="\n".join(f"    <string>{_xml_escape(part)}</string>" for part in cmd),
            log=_xml_escape(str(_service_log_file())),
        )
        enable_commands = [f"launchctl load -w {target}"]
        platform = "macos-launchd"
    elif os.name == "nt":
        target = config.IVYEA_DIR / "start-ivyea-agent.ps1"
        content = " ".join(_powershell_quote(part) for part in cmd)
        enable_commands = [
            "schtasks /Create /TN IvyeaAgent /SC ONLOGON /TR "
            + _powershell_quote(f"powershell -NoProfile -ExecutionPolicy Bypass -File {target}")
        ]
        platform = "windows-task-scheduler"
    else:
        target = Path.home() / ".config" / "systemd" / "user" / "ivyea-agent.service"
        content = "\n".join([
            "[Unit]",
            "Description=Ivyea Agent local API",
            "",
            "[Service]",
            "ExecStart=" + " ".join(cmd),
            "Restart=on-failure",
            "RestartSec=3",
            f"Environment=IVYEA_HOME={config.IVYEA_DIR}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ])
        enable_commands = ["systemctl --user daemon-reload", "systemctl --user enable --now ivyea-agent.service"]
        platform = "linux-systemd-user"
    return {
        "ok": True,
        "platform": platform,
        "target": str(target),
        "content": content,
        "enable_commands": enable_commands,
    }


def write_autostart(host: str = "127.0.0.1", port: int = 8765) -> dict[str, Any]:
    plan = autostart_files(host, port)
    target = Path(str(plan["target"])).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(plan["content"]), encoding="utf-8")
    return {**plan, "written": True, "target": str(target)}


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _powershell_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


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


def ops_bootstrap(host: str = "127.0.0.1", port: int = 8765) -> dict[str, Any]:
    """Return a local integration contract for IvyeaOps installers/settings pages."""
    safe_host = host or "127.0.0.1"
    safe_port = int(port or 8765)
    base_url = f"http://{safe_host}:{safe_port}"
    info = install_info()
    doctor = install_doctor(info)
    return {
        "ok": bool(doctor.get("ok")),
        "name": "ivyea-agent",
        "version": __version__,
        "mode": "local-http-plus-stdio-mcp",
        "install": {
            "linux_macos": "curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash",
            "windows": "iwr https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.ps1 -UseBasicParsing | iex",
            "offline": "下载 release 离线包后执行 bash install.sh 或 powershell -ExecutionPolicy Bypass -File .\\install.ps1",
        },
        "start": {
            "command": "ivyea",
            "args": ["serve", "--host", safe_host, "--port", str(safe_port)],
            "local_default_auth_required": False,
            "remote_bind_requires": ["--allow-remote", "--api-token/IVYEA_API_TOKEN"],
        },
        "urls": {
            "base": base_url,
            "health": f"{base_url}/health",
            "manifest": f"{base_url}/v1/manifest",
            "openapi": f"{base_url}/v1/openapi.json",
            "system_status": f"{base_url}/v1/system/status",
            "system_doctor": f"{base_url}/v1/system/doctor",
            "service_status": f"{base_url}/v1/system/service/status",
            "service_logs": f"{base_url}/v1/system/service/logs",
        },
        "mcp": {
            "transport": "stdio",
            "command": "ivyea",
            "args": ["mcp", "serve"],
            "read_only": True,
        },
        "startup_templates": _startup_templates(safe_host, safe_port),
        "service_management": {
            "status": "ivyea self service-status",
            "start": f"ivyea self service-start --host {safe_host} --port {safe_port}",
            "stop": "ivyea self service-stop",
            "logs": "ivyea self service-logs",
            "autostart": f"ivyea self service-autostart --host {safe_host} --port {safe_port}",
            "pid_file": str(_pid_file()),
            "log_file": str(_service_log_file()),
        },
        "recommended_flow": [
            "安装 ivyea-agent",
            "运行 ivyea self doctor 检查 Python/PATH/数据目录/检索依赖",
            "运行 ivyea retrieval sync 初始化或同步本地知识/记忆索引",
            f"启动本地服务：ivyea serve --host {safe_host} --port {safe_port}",
            f"IvyeaOps 读取 {base_url}/v1/manifest 和 /v1/openapi.json 自动发现能力",
        ],
        "info": info,
        "doctor": {
            "ok": bool(doctor.get("ok")),
            "checks": doctor.get("checks") or [],
            "next_steps": doctor.get("next_steps") or [],
        },
    }


def _startup_templates(host: str, port: int) -> dict[str, str]:
    command = f"ivyea serve --host {host} --port {port}"
    return {
        "systemd_user": "\n".join([
            "[Unit]",
            "Description=Ivyea Agent local API",
            "",
            "[Service]",
            "ExecStart=" + command,
            "Restart=on-failure",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=default.target",
        ]),
        "launchd_plist_hint": "ProgramArguments: ivyea, serve, --host, "
        f"{host}, --port, {port}; KeepAlive=true; RunAtLoad=true",
        "windows_task": "powershell -NoProfile -WindowStyle Hidden -Command "
        f"\"ivyea serve --host {host} --port {port}\"",
    }


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


def render_ops_bootstrap(data: dict[str, Any] | None = None) -> str:
    data = data or ops_bootstrap()
    start = data.get("start") or {}
    urls = data.get("urls") or {}
    mcp = data.get("mcp") or {}
    lines = [
        "IvyeaOps Bootstrap",
        "",
        f"- ok: {data.get('ok')}",
        f"- version: {data.get('version')}",
        f"- mode: {data.get('mode')}",
        f"- start: {start.get('command')} {' '.join(start.get('args') or [])}",
        f"- health: {urls.get('health')}",
        f"- manifest: {urls.get('manifest')}",
        f"- openapi: {urls.get('openapi')}",
        f"- mcp: {mcp.get('command')} {' '.join(mcp.get('args') or [])} ({mcp.get('transport')}, read_only={mcp.get('read_only')})",
    ]
    flow = data.get("recommended_flow") or []
    if flow:
        lines.extend(["", "Recommended Flow"])
        lines.extend(f"{i}. {step}" for i, step in enumerate(flow, start=1))
    templates = data.get("startup_templates") or {}
    if templates:
        lines.extend(["", "Startup Templates"])
        lines.append("- systemd_user: ~/.config/systemd/user/ivyea-agent.service")
        lines.append("- launchd: use launchd_plist_hint in ~/Library/LaunchAgents/com.ivyea.agent.plist")
        lines.append("- windows_task: use windows_task in Task Scheduler or PowerShell")
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


def render_service_status(data: dict[str, Any] | None = None) -> str:
    data = data or service_status()
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    lines = [
        "Ivyea Agent Service",
        "",
        f"- running: {data.get('running')}",
        f"- pid: {data.get('pid') or '-'}",
        f"- base_url: {data.get('base_url')}",
        f"- pid_file: {data.get('pid_file')}",
        f"- log_file: {data.get('log_file')}",
        f"- health: {health.get('ok')}",
    ]
    if data.get("stale_pid"):
        lines.append("- warning: stale pid file detected")
    if health.get("error"):
        lines.append(f"- health_error: {health.get('error')}")
    return "\n".join(lines)


def render_service_logs(data: dict[str, Any]) -> str:
    lines = ["Ivyea Agent Service Logs", "", f"- path: {data.get('path')}"]
    if not data.get("ok"):
        lines.append(f"- error: {data.get('error')}")
        return "\n".join(lines)
    text = data.get("text") or ""
    if text:
        lines.extend(["", text])
    else:
        lines.append("- empty")
    return "\n".join(lines)


def render_autostart(data: dict[str, Any]) -> str:
    lines = [
        "Ivyea Agent Autostart",
        "",
        f"- platform: {data.get('platform')}",
        f"- target: {data.get('target')}",
        f"- written: {data.get('written', False)}",
    ]
    commands = data.get("enable_commands") or []
    if commands:
        lines.extend(["", "Enable commands:"])
        lines.extend(f"- {cmd}" for cmd in commands)
    return "\n".join(lines)
