"""用户钩子（对标 Claude Code hooks，轻量版）。

~/.ivyea/hooks.json 配置在事件上运行的 shell 命令，opt-in：没有该文件就零开销。
事件：user_prompt / session_start / session_end / pre_tool_use / post_tool_use / stop。
条目两种写法（可混用）：
  "user_prompt": ["notify.sh"]                                    ← 字符串：整事件都跑
  "pre_tool_use": [{"matcher": "run_command|write_file",
                    "command": "guard.sh", "timeout": 10}]         ← dict：matcher 是对
    工具名的 re.fullmatch 正则；工具事件上的字符串条目默认跳过只读工具（避免并行
    只读 × 子进程开销叠加），要拦只读必须显式写 matcher。
事件 payload 以 JSON 经环境变量 IVYEA_HOOK_EVENT / IVYEA_HOOK_PAYLOAD 传入。
普通钩子失败/超时绝不影响主流程（best-effort、有超时上限）。

pre_tool_use 是决策钩子（fire_decision）：exit code 2 = 拒绝该工具调用（stderr 作
拒绝理由）；stdout 输出 JSON {"decision": "block", "reason": "..."} 也算拒绝；
其余一切（exit 0/1、超时、崩溃、非 JSON 输出）一律放行——fail-open，钩子坏了不锁死 agent。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from functools import lru_cache

from . import config

_TIMEOUT = 15
_TIMEOUT_MAX = 60
VALID_EVENTS = {"user_prompt", "session_start", "session_end",
                "pre_tool_use", "post_tool_use", "stop"}
_TOOL_EVENTS = {"pre_tool_use", "post_tool_use"}


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


def _normalize(entries) -> list[dict]:
    """把事件条目统一成 dict：{"command", "matcher", "timeout"}。非法条目丢弃。"""
    out = []
    for e in entries or []:
        if isinstance(e, str):
            if e.strip():
                out.append({"command": e, "matcher": "", "timeout": _TIMEOUT})
        elif isinstance(e, dict) and isinstance(e.get("command"), str) and e["command"].strip():
            try:
                t = min(max(1, int(e.get("timeout") or _TIMEOUT)), _TIMEOUT_MAX)
            except (TypeError, ValueError):
                t = _TIMEOUT
            out.append({"command": e["command"], "matcher": str(e.get("matcher") or ""), "timeout": t})
    return out


def _matches(entry: dict, event: str, tool_name: str, readonly: bool) -> bool:
    """条目是否命中：非工具事件全命中；工具事件按 matcher 正则匹配工具名，
    没写 matcher 的默认只拦非只读工具（readonly 由调用方传入，避免反向依赖 agent_tools）。"""
    if event not in _TOOL_EVENTS:
        return True
    matcher = entry.get("matcher") or ""
    if matcher:
        try:
            return re.fullmatch(matcher, tool_name) is not None
        except re.error:
            return False
    return not readonly


def _entries(event: str, tool_name: str = "", readonly: bool = False) -> list[dict]:
    if event not in VALID_EVENTS:
        return []
    return [e for e in _normalize(_load().get(event))
            if _matches(e, event, tool_name, readonly)]


def _hook_env(event: str, payload: dict | None) -> dict:
    env = dict(os.environ)
    env["IVYEA_HOOK_EVENT"] = event
    env["IVYEA_HOOK_PAYLOAD"] = json.dumps(payload or {}, ensure_ascii=False)
    return env


def _shell(cmd: str) -> list[str]:
    return ["cmd", "/c", cmd] if os.name == "nt" else ["bash", "-c", cmd]


def fire(event: str, payload: dict | None = None, *,
         tool_name: str = "", readonly: bool = False) -> None:
    """运行某事件下配置的所有命令；任何失败都吞掉，不打断主流程。"""
    entries = _entries(event, tool_name, readonly)
    if not entries:
        return
    env = _hook_env(event, payload)
    for e in entries:
        try:
            subprocess.run(_shell(e["command"]), env=env, timeout=e["timeout"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue


def fire_decision(event: str, payload: dict | None = None, *,
                  tool_name: str = "", readonly: bool = False) -> tuple[bool, str]:
    """决策钩子（pre_tool_use）：返回 (放行?, 拒绝理由)。

    协议对齐 Claude Code：exit code 2 = 拒绝（stderr 作 reason）；
    exit 0 + stdout JSON {"decision": "block"|"deny", "reason": ...} 也拒绝；
    其余一切（exit 0/1、超时、崩溃、非 JSON stdout）→ 放行（fail-open）。
    多条钩子按序执行，第一条拒绝即短路。"""
    entries = _entries(event, tool_name, readonly)
    if not entries:
        return True, ""
    env = _hook_env(event, payload)
    for e in entries:
        try:
            proc = subprocess.run(_shell(e["command"]), env=env, timeout=e["timeout"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except (OSError, subprocess.SubprocessError):
            continue                                  # 钩子自身坏了 → 放行
        if proc.returncode == 2:
            reason = (proc.stderr or "").strip()[:500] or "hook 拒绝（exit 2）"
            return False, reason
        out = (proc.stdout or "").strip()
        if out:
            try:
                data = json.loads(out)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("decision") in ("block", "deny"):
                return False, str(data.get("reason") or "hook 拒绝")[:500]
    return True, ""
