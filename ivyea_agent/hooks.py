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

from . import config, subproc_env

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
            out.append({"command": e["command"], "matcher": str(e.get("matcher") or ""),
                        "timeout": t,
                        # 环境相关三件套跟着条目走：不同 hook 需要的东西不一样，
                        # 给全局开一个口子等于没清洗。
                        #
                        # 只有写了 `inherit_env: false` 才收紧；没写就继承全部
                        # 环境（旧行为），env / env_passthrough 一律叠加。
                        # 理由同 mcp_client：不能替用户做他没做过的决定。
                        "env": dict(e.get("env") or {}),
                        "env_passthrough": [str(k) for k in (e.get("env_passthrough") or [])],
                        "inherit_env": bool(e.get("inherit_env", True))})
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


def _hook_env(event: str, payload: dict | None, entry: dict | None = None) -> dict:
    """hook 子进程的环境：白名单 + 该条目显式声明的，外加 hook 自己的两个约定键。

    以前是 `dict(os.environ)` —— hook 脚本能读到本机全部密钥。hook 是本机作者
    自己写的，风险低于第三方 MCP server，但没有理由让一个"发个通知"的脚本
    看得见 DEEPSEEK_API_KEY。要哪个就在 hooks.json 的条目里写哪个。
    """
    entry = entry or {}
    return subproc_env.build_env(
        entry.get("env"),
        passthrough=entry.get("env_passthrough"),
        inherit_all=bool(entry.get("inherit_env")),
        # IVYEA_HOOK_* 是 hook 的 API，必须盖过配置，不能被覆写掉。
        extra={
            "IVYEA_HOOK_EVENT": event,
            "IVYEA_HOOK_PAYLOAD": json.dumps(payload or {}, ensure_ascii=False),
        },
    )


def _shell(cmd: str) -> list[str]:
    return ["cmd", "/c", cmd] if os.name == "nt" else ["bash", "-c", cmd]


def fire(event: str, payload: dict | None = None, *,
         tool_name: str = "", readonly: bool = False) -> None:
    """运行某事件下配置的所有命令；任何失败都吞掉，不打断主流程。"""
    entries = _entries(event, tool_name, readonly)
    if not entries:
        return
    for e in entries:
        try:
            subprocess.run(_shell(e["command"]), env=_hook_env(event, payload, e),
                           timeout=e["timeout"],
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
    for e in entries:
        try:
            proc = subprocess.run(_shell(e["command"]), env=_hook_env(event, payload, e),
                                  timeout=e["timeout"],
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
