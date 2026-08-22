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
        _check("飞书", _feishu),
        _check("店铺巡检", _patrol),
        _check("待审批动作", _approvals),
        _check("领星写开关", _operate_switch),
        _check("数据源健康", _data_health),
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
    detail = f"{s.get('label')} · {s.get('model')} · {key_env}"
    health = config.main_brain_health()   # 含 oauth 凭据过期判断
    if not health.get("ok"):
        return Check("模型配置", "warn", detail + f"，{health.get('status')}", health.get("hint", "运行 `ivyea model` 配置/切换"))
    return Check("模型配置", "ok", detail + "，主脑可用")


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
        return Check("MCP", "warn", "未配置 MCP 服务器",
                     "ASIN 深度审计等取数任务需要至少一个 trusted 数据源 MCP："
                     "`ivyea mcp add`（配好后选“信任/免审批”）")
    trusted = [name for name, spec in servers.items() if isinstance(spec, dict) and spec.get("trusted")]
    try:
        from . import mcp_write
        writable = [name for name, spec in servers.items() if not mcp_write.validate_spec(spec)]
    except Exception:
        writable = []
    # trusted 服务器 = 无人值守取数（ASIN 审计等）可直接调用的数据源。
    if not trusted:
        return Check("MCP", "warn", f"{len(servers)} 个服务器，但没有 trusted（免审批）数据源",
                     "ASIN 审计走 ivyea-agent 需要一个 trusted 数据源 MCP："
                     "`ivyea mcp add`（选“信任/免审批”），或在 mcp.json 里给该服务器加 \"trusted\": true")
    detail = f"{len(servers)} 个服务器，{len(trusted)} 个 trusted 数据源"
    if writable:
        detail += f"，{len(writable)} 个具备写入映射"
    return Check("MCP", "ok", detail)


def _feishu() -> Check:
    from . import feishu_client

    if not feishu_client.is_configured():
        return Check("飞书", "warn", "未配置应用凭据",
                     "在 ~/.ivyea/.env 设 IVYEA_FEISHU_APP_ID / IVYEA_FEISHU_APP_SECRET")
    chat = feishu_client.default_chat_id()
    if not chat:
        return Check("飞书", "warn", "已配凭据，但没有默认会话",
                     "在 settings.json 设 feishu_default_chat_id，否则告警不知道发给谁")
    return Check("飞书", "ok", f"已配置，默认会话 {chat[:14]}…")


def _patrol() -> Check:
    from . import schedule

    jobs = [j for j in schedule.load().get("jobs", [])
            if str(j.get("task", "")).startswith("store_")]
    if not jobs:
        return Check("店铺巡检", "warn", "未注册任何巡检任务",
                     "`ivyea schedule set l1 store_l1 --every-minutes 20 --sid <SID>`；"
                     "只注册不等于会跑，还要装 deploy/systemd/ivyea-schedule.timer")
    enabled = [j for j in jobs if j.get("enabled", True)]
    never = [j["name"] for j in enabled if not j.get("last_run")]
    detail = f"{len(enabled)}/{len(jobs)} 个任务启用"
    if never:
        # 注册了却从没跑过，多半是 timer 没装 —— 这种"以为在跑其实没跑"最危险
        return Check("店铺巡检", "warn", f"{detail}；{len(never)} 个从未执行过",
                     "检查触发器：`systemctl list-timers ivyea-schedule.timer`；"
                     f"从未跑过的：{'、'.join(never[:3])}")
    return Check("店铺巡检", "ok", detail)


def _approvals() -> Check:
    from . import approvals

    summary = approvals.summary()
    pending = summary.get(approvals.PENDING, 0)
    stuck = summary.get(approvals.APPROVED, 0)
    failed = summary.get(approvals.FAILED, 0)
    if failed:
        return Check("待审批动作", "warn", f"{failed} 条执行失败",
                     "`ivyea approval list --state failed` 看原因；"
                     "写入失败会自动熔断关掉写开关")
    if stuck:
        return Check("待审批动作", "warn", f"{stuck} 条已批准但未执行",
                     "多半是写开关没开：`ivyea lingxing operate on` 后 "
                     "`ivyea approval execute <ID>`")
    if pending:
        return Check("待审批动作", "ok", f"{pending} 条待你处理")
    return Check("待审批动作", "ok", "无积压")


def _operate_switch() -> Check:
    from . import lingxing_write

    if not lingxing_write.operate_active():
        return Check("领星写开关", "ok", "关闭（批准的动作会记为待执行）")
    exp = config.get_setting("lingxing_operate_expires_at", 0)
    left = ""
    if exp:
        import time as _t
        left = f"，约 {max(0, int((float(exp) - _t.time()) / 60))} 分钟后自动关闭"
    return Check("领星写开关", "warn", f"开启中{left}",
                 "开着期间批准的动作会真实写入领星。不用了就 `ivyea lingxing operate off`")


def _data_health() -> Check:
    from . import reliability

    snap = reliability.snapshot()
    bad = {k: v for k, v in snap.items() if int((v or {}).get("consecutive") or 0) >= 2}
    if not bad:
        return Check("数据源健康", "ok", "无连续失败")
    worst = max(bad.items(), key=lambda kv: kv[1].get("consecutive", 0))
    return Check("数据源健康", "warn",
                 f"{len(bad)} 项连续取数失败，最严重 {worst[0]} 连续 {worst[1]['consecutive']} 次",
                 f"原因：{str(worst[1].get('detail') or '')[:120]}")


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
