"""用户钩子（对标 Claude Code hooks，轻量版）。

~/.ivyea/hooks.json 配置在事件上运行的 shell 命令，opt-in：没有该文件就零开销。
格式：{"user_prompt": ["cmd ..."], "session_start": [...], "session_end": [...]}
事件 payload 以 JSON 经环境变量 IVYEA_HOOK_EVENT / IVYEA_HOOK_PAYLOAD 传入。
钩子失败/超时绝不影响主流程（best-effort、有超时上限）。
"""
from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache

from . import config

_TIMEOUT = 15
VALID_EVENTS = {"user_prompt", "session_start", "session_end"}


def hooks_file():
    return config.IVYEA_DIR / "hooks.json"


@lru_cache(maxsize=1)
def _load() -> dict:
    p = hooks_file()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def reload() -> None:
    _load.cache_clear()


def enabled() -> bool:
    return bool(_load())


def fire(event: str, payload: dict | None = None) -> None:
    """运行某事件下配置的所有命令；任何失败都吞掉，不打断主流程。"""
    if event not in VALID_EVENTS:
        return
    cmds = _load().get(event) or []
    if not cmds:
        return
    env = dict(os.environ)
    env["IVYEA_HOOK_EVENT"] = event
    env["IVYEA_HOOK_PAYLOAD"] = json.dumps(payload or {}, ensure_ascii=False)
    for cmd in cmds:
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        shell = ["cmd", "/c", cmd] if os.name == "nt" else ["bash", "-c", cmd]
        try:
            subprocess.run(shell, env=env, timeout=_TIMEOUT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
