"""Environment and project health checks for Ivyea Agent."""
from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, knowledge, profiles


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str
    fix: str = ""


def _check(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return Check(name, "fail", f"检查失败：{e}")


def run_checks() -> list[Check]:
    config.load_env()
    return [
        _check("Python", _python),
        _check("数据目录", _data_dir),
        _check("模型配置", _model),
        _check("运营画像", _profiles),
        _check("知识库", _knowledge),
        _check("规则引擎依赖", _rule_engine_deps),
        _check("curl", _curl),
        _check("领星 OpenAPI", _lingxing),
        _check("MCP", _mcp),
        _check("磁盘空间", _disk),
    ]


def _python() -> Check:
    v = sys.version_info
    if v < (3, 9):
        return Check("Python", "fail", f"{sys.version.split()[0]}，需要 >=3.9")
    return Check("Python", "ok", sys.version.split()[0])


def _data_dir() -> Check:
    config.ensure_dirs()
    p = config.IVYEA_DIR
    writable = p.exists() and p.is_dir()
    try:
        test = p / ".doctor-write-test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
    except Exception:
        writable = False
    if not writable:
        return Check("数据目录", "fail", f"{p} 不可写", "检查权限或设置 IVYEA_HOME")
    return Check("数据目录", "ok", str(p))


def _model() -> Check:
    s = config.get_model_config()
    key_env = s.get("key_env") or ""
    has_key = bool(config.get_active_key())
    detail = f"{s.get('label')} · {s.get('model')} · {key_env}"
    if not has_key:
        return Check("模型配置", "warn", detail + "，key 未配置", "运行 `ivyea model` 配置 API key")
    return Check("模型配置", "ok", detail + "，key 已配置")


def _knowledge() -> Check:
    builtin = knowledge.list_builtin_cards()
    user = knowledge.list_user_cards()
    if not builtin:
        return Check("知识库", "fail", "未找到内置知识卡")
    return Check("知识库", "ok", f"{len(builtin)} 张内置知识卡，{len(user)} 张用户知识卡")


def _profiles() -> Check:
    rows = profiles.list_profiles()
    configured = [name for name, p in rows if p.get("target_acos") is not None or p.get("protected_terms") or p.get("core_terms")]
    if not configured:
        return Check("运营画像", "warn", "未配置目标 ACOS/保护词/核心词",
                     "运行 `ivyea profile set default --target-acos 0.3 --protected 品牌词`")
    return Check("运营画像", "ok", f"{len(configured)} 个画像已配置")


def _rule_engine_deps() -> Check:
    missing = []
    for mod in ("pandas", "openpyxl"):
        if importlib.util.find_spec(mod) is None:
            missing.append(mod)
    if missing:
        return Check("规则引擎依赖", "warn", "缺少 " + ", ".join(missing),
                     "运行 `pip install pandas openpyxl` 或重新安装 ivyea-agent 依赖")
    return Check("规则引擎依赖", "ok", "pandas/openpyxl 可用")


def _curl() -> Check:
    p = shutil.which("curl")
    if not p:
        return Check("curl", "warn", "未找到 curl", "Listing/网页采集可能受限，安装 curl")
    return Check("curl", "ok", p)


def _lingxing() -> Check:
    try:
        from .lingxing_openapi import is_configured
    except Exception as e:  # noqa: BLE001
        return Check("领星 OpenAPI", "warn", f"模块加载失败：{e}")
    if not is_configured():
        return Check("领星 OpenAPI", "warn", "未配置", "运行 `ivyea lingxing setup`")
    return Check("领星 OpenAPI", "ok", "已配置")


def _mcp() -> Check:
    servers = config.load_mcp().get("mcpServers", {})
    if not servers:
        return Check("MCP", "warn", "未配置 MCP 服务器")
    try:
        from . import mcp_write
        writable = [name for name, spec in servers.items() if not mcp_write.validate_spec(spec)]
    except Exception:
        writable = []
    if not writable:
        return Check("MCP", "warn", f"{len(servers)} 个服务器，但未发现完整 writeActions 映射",
                     "运行 `ivyea mcp template` 查看示例，`ivyea mcp validate <名称>` 校验")
    return Check("MCP", "ok", f"{len(servers)} 个服务器，{len(writable)} 个具备写入映射")


def _disk() -> Check:
    usage = shutil.disk_usage(str(Path.home()))
    free_gb = usage.free / (1024 ** 3)
    used_pct = usage.used / usage.total
    detail = f"可用 {free_gb:.1f}G，已用 {used_pct:.0%}"
    if free_gb < 2 or used_pct > 0.95:
        return Check("磁盘空间", "fail", detail, "清理缓存、日志或容器镜像")
    if free_gb < 8 or used_pct > 0.85:
        return Check("磁盘空间", "warn", detail, "建议保留 8G+ 可用空间")
    return Check("磁盘空间", "ok", detail)


def render(checks: list[Check]) -> str:
    icon = {"ok": "OK", "warn": "!!", "fail": "XX"}
    lines = ["Ivyea Agent Doctor", ""]
    for c in checks:
        lines.append(f"{icon.get(c.status, '??')} {c.name}: {c.detail}")
        if c.fix:
            lines.append(f"   修复: {c.fix}")
    fails = sum(1 for c in checks if c.status == "fail")
    warns = sum(1 for c in checks if c.status == "warn")
    lines.append("")
    lines.append(f"结果: {fails} fail / {warns} warn / {len(checks) - fails - warns} ok")
    return "\n".join(lines)
