"""版本更新检测 + 一键更新（对标 Claude Code 的启动更新提示）。

- check_latest(): 启动时**非阻塞**检测——读本地缓存立即返回(可能提示)，缓存过期(>6h)则起后台线程
  刷新供下次用；离线/超时优雅降级(latest=None)。
- check_now(): /update 命令用的同步检测(带超时)。
- do_update(): 执行更新——源码 git 仓 → git pull；否则 → pip/pipx 升级(复用 self_manage.upgrade_plan)。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, config

_REPO = "Hector-xue/ivyea-agent"
_TTL = 6 * 3600   # 缓存有效期（秒）


def _cache_file() -> Path:
    return config.IVYEA_DIR / "update_check.json"


def _norm(v: str) -> tuple:
    """'v1.2.3' / '1.2.3' → (1,2,3)，便于比较。"""
    parts = []
    for p in (v or "").strip().lstrip("vV").split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def has_update(current: str, latest: str | None) -> bool:
    return bool(latest) and _norm(latest) > _norm(current)


def _fetch_latest(timeout: float = 3.0) -> str | None:
    """拉 GitHub 最新 release 的 tag_name；任何异常/离线返回 None。"""
    try:
        import httpx
        r = httpx.get(f"https://api.github.com/repos/{_REPO}/releases/latest",
                      timeout=timeout, follow_redirects=True,
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "ivyea-agent"})
        if r.status_code == 200:
            return (r.json().get("tag_name") or "").strip() or None
    except Exception:
        return None
    return None


def _write_cache(latest: str | None) -> None:
    try:
        config.ensure_dirs()
        _cache_file().write_text(json.dumps({"latest": latest, "checked_at": time.time()}),
                                 encoding="utf-8")
    except Exception:
        pass


def _read_cache() -> dict:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def check_latest() -> dict:
    """启动用**非阻塞**检测：读缓存立即返回；缓存过期则后台刷新(供下次)。
    返回 {current, latest, has_update}。"""
    current = __version__
    data = _read_cache()
    if time.time() - float(data.get("checked_at", 0)) >= _TTL:
        threading.Thread(target=lambda: _write_cache(_fetch_latest()), daemon=True).start()
    latest = data.get("latest")
    return {"current": current, "latest": latest, "has_update": has_update(current, latest)}


def check_now(timeout: float = 5.0) -> dict:
    """同步检测(拉网络，写缓存)——供 /update 命令用。"""
    current = __version__
    latest = _fetch_latest(timeout=timeout)
    _write_cache(latest)
    return {"current": current, "latest": latest, "has_update": has_update(current, latest)}


def _source_repo() -> Path | None:
    """本包若在 git 源码仓里(源码安装)→返回仓根；否则 None。"""
    try:
        from . import git_workflow
        return git_workflow.repo_root(Path(__file__).resolve().parent)
    except Exception:
        return None


def update_commands() -> list[list[str]]:
    """本机的更新命令（跨平台，**不依赖 bash**——Windows 没有 bash，旧版把命令包成
    `bash -lc` 直接 WinError 2）：
    - 源码仓 → `git pull`（git 跨平台在 PATH）。
    - pipx 安装 → 直接跑 `pipx upgrade ivyea-agent`（解析 pipx 可执行文件绝对路径，
      Windows 下即 pipx.exe）。
    - 其余（venv / ivyea-runtime / unknown）→ 用当前解释器的 pip 升级，无 PATH/shell 依赖。
    """
    root = _source_repo()
    if root is not None:
        return [["git", "-C", str(root), "pull", "--ff-only"]]
    try:
        from . import self_manage
        info = self_manage.install_info()
        if info.get("method") == "pipx":
            pipx = shutil.which("pipx") or (info.get("pipx") or "") or "pipx"
            return [[pipx, "upgrade", "ivyea-agent"]]
    except Exception:
        pass
    return [[sys.executable, "-m", "pip", "install", "--upgrade", "ivyea-agent"]]


def do_update() -> tuple[bool, str]:
    """执行更新命令，返回 (ok, 合并输出)。"""
    cmds = update_commands()
    if not cmds:
        return False, "没有可用的更新命令。"
    out: list[str] = []
    for cmd in cmds:
        shown = ([Path(cmd[0]).name] + cmd[1:]) if cmd else cmd   # 首项只显示程序名(路径太长)
        out.append("$ " + " ".join(shown))
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, timeout=300)
            out.append((r.stdout or "").strip())
            if r.returncode != 0:
                return False, "\n".join(out)
        except Exception as e:   # noqa: BLE001
            return False, "\n".join(out) + f"\n执行失败：{e}"
    return True, "\n".join(p for p in out if p)
