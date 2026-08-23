"""宿主机上的两个常驻件：巡检触发器与飞书接收端。

**为什么要有这个模块**：这两样东西以前只以"仓库里的 systemd 文件"和"开发机上的
一个目录"的形式存在，`pip install` 拿到的包里根本没有。后果是配置全填对了，
界面全绿，然后——巡检任务注册了但永远不触发，卡片按钮点了什么都不发生。
用户没有任何线索，因为**缺的东西不在他机器上，报错也就无从产生**。

所以单元内容写在代码里（跟着 wheel 走），安装收成一个动作，CLI 和网页共用。

**平台策略**（照抄 ``self_manage.autostart_files`` 的成例，那套已经在用）：

- Linux + systemd：**端到端装完并启动**，用户什么都不用敲。
- macOS / Windows：把文件写好、把该执行的命令**原样给出来**，由用户执行。
  这里刻意不代劳：launchctl / schtasks 的失败模式很难在没有那台机器的情况下
  判断对错，猜着写不如说清楚。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config

SYSTEMD_DIR = Path("/etc/systemd/system")

#: 巡检触发器。**时间写在 schedule.json 而不是 timer 里** —— 改巡检频率不用动 systemd。
SCHEDULE_SERVICE = "ivyea-schedule.service"
SCHEDULE_TIMER = "ivyea-schedule.timer"

_SCHEDULE_SERVICE_UNIT = """[Unit]
Description=IvyeaAgent scheduled tasks (store patrol / alerts / approvals sweep)
After=network-online.target

[Service]
Type=oneshot
User={user}
Environment=HOME={home}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONUTF8=1
ExecStart={python} -m ivyea_agent.cli schedule run-due
# 单次 run-due 最长 30 分钟：一次可能跑完多个到期任务（重启后 Persistent=true
# 的补跑会把 L1/L2/早报凑到同一次），再叠上报表缓存过期那天的冷启动。
TimeoutStartSec=1800
Nice=10

[Install]
WantedBy=multi-user.target
"""

_SCHEDULE_TIMER_UNIT = """[Unit]
Description=Wake IvyeaAgent scheduled tasks every 5 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
AccuracySec=30s
# 停机期间错过的执行，开机后补跑一次，而不是静默跳过。
Persistent=true

[Install]
WantedBy=timers.target
"""


def _systemd() -> bool:
    return os.name != "nt" and sys.platform != "darwin" and bool(shutil.which("systemctl"))


def _run(cmd: list[str], timeout: float = 30.0) -> dict[str, Any]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        return {"cmd": " ".join(cmd), "ok": False, "detail": str(exc)}
    return {"cmd": " ".join(cmd), "ok": p.returncode == 0,
            "detail": ((p.stdout or "") + (p.stderr or "")).strip()[:400]}


def _unit_active(name: str) -> str:
    if not _systemd():
        return "unknown"
    p = _run(["systemctl", "is-active", name], timeout=5.0)
    return (p["detail"] or "unknown").splitlines()[0] if p["detail"] else "unknown"


# ── 巡检触发器 ──────────────────────────────────────────────────────────────
def systemd_timer_running() -> bool:
    """**只问 systemd**。serve_workers 用它判断"要不要让位"——
    掺进进程内工人的状态就成了自指（见那边的说明）。"""
    return _systemd() and _unit_active(SCHEDULE_TIMER) == "active"


def schedule_status() -> dict[str, Any]:
    """**注册了任务 ≠ 会跑。** 触发器没装的话，界面上开的巡检永远不会被执行，
    而且不会有任何报错——这是最难自己发现的一类故障。"""
    from . import schedule

    jobs = [j for j in schedule.load().get("jobs", []) if j.get("enabled", True)]

    # **内建节拍器要在平台分支之前判**（同 feishu_setup._relay_status 的理由）：
    # Windows / macOS 上它就是唯一的落法，报"无法判定"等于告诉用户功能没生效。
    from . import serve_workers
    inproc = serve_workers.status().get("scheduler") or {}
    if inproc.get("running"):
        return {"installed": True, "running": True, "state": "builtin",
                "jobs": len(jobs), "can_install": _systemd(), "builtin": True,
                "detail": f"已随 IvyeaAgent 服务内建运行（{len(jobs)} 个任务在册）"}

    if not _systemd():
        return {"installed": None, "running": None, "jobs": len(jobs),
                "detail": "本机没有 systemd；巡检会随 IvyeaAgent 服务内建运行，"
                          "重启服务后生效（或用计划任务每 5 分钟跑一次 "
                          "`ivyea schedule run-due`）",
                "can_install": False}

    state = _unit_active(SCHEDULE_TIMER)
    running = state == "active"
    return {
        "installed": (SYSTEMD_DIR / SCHEDULE_TIMER).exists(),
        "running": running, "state": state, "jobs": len(jobs), "can_install": True,
        "detail": (f"{SCHEDULE_TIMER} 运行中（{len(jobs)} 个任务在册）" if running
                   else f"{SCHEDULE_TIMER} 未启用 —— 注册的 {len(jobs)} 个任务不会被触发"),
    }


def install_schedule() -> dict[str, Any]:
    """装上巡检触发器。Linux 端到端装完；其它平台给出文件与命令。"""
    python = sys.executable or "python3"
    home = str(Path.home())
    user = "root" if os.geteuid() == 0 else (os.environ.get("USER") or "")  # type: ignore[attr-defined]
    service = _SCHEDULE_SERVICE_UNIT.format(python=python, home=home, user=user or "root")

    if not _systemd():
        target = config.IVYEA_DIR / "ivyea-schedule.txt"
        target.write_text(
            "每 5 分钟执行一次：\n"
            f"    {python} -m ivyea_agent.cli schedule run-due\n\n"
            "Windows：任务计划程序 → 创建基本任务 → 重复间隔 5 分钟\n"
            "macOS：launchd 的 StartInterval=300\n", encoding="utf-8")
        return {"ok": False, "manual": True, "file": str(target),
                "hint": f"本机没有 systemd。请让系统每 5 分钟执行一次："
                        f"{python} -m ivyea_agent.cli schedule run-due"}

    steps = []
    try:
        (SYSTEMD_DIR / SCHEDULE_SERVICE).write_text(service, encoding="utf-8")
        (SYSTEMD_DIR / SCHEDULE_TIMER).write_text(_SCHEDULE_TIMER_UNIT, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"写入 {SYSTEMD_DIR} 失败（需要 root）：{exc}"}
    steps.append(_run(["systemctl", "daemon-reload"]))
    steps.append(_run(["systemctl", "enable", "--now", SCHEDULE_TIMER]))
    st = schedule_status()
    return {"ok": bool(st.get("running")), "steps": steps, "status": st}


# ── 飞书接收端 ──────────────────────────────────────────────────────────────
def install_relay(*, install_sdk: bool = True) -> dict[str, Any]:
    """装上飞书接收端：缺 SDK 就先装 SDK，再写单元并启动。

    ``install_sdk`` 默认开：网页用户没有终端，"请自行 pip install" 对他等于
    "这个功能你用不了"。
    """
    from . import feishu_relay

    steps: list[dict[str, Any]] = []
    if install_sdk and not feishu_relay.sdk_available():
        # 装进**当前解释器**的环境，与 agent 自身同一份 —— 装到别处等于没装
        steps.append(_run([sys.executable, "-m", "pip", "install", "--no-input",
                           "lark-oapi>=1.4"], timeout=600.0))
        if not feishu_relay.sdk_available():
            return {"ok": False, "steps": steps,
                    "error": "飞书 SDK 安装失败", "hint": feishu_relay.SDK_HINT}

    if not _systemd():
        target = config.IVYEA_DIR / "start-ivyea-feishu-relay.txt"
        cmd = f"{sys.executable} -m ivyea_agent.feishu_relay"
        target.write_text(f"常驻运行这一条即可：\n    {cmd}\n", encoding="utf-8")
        return {"ok": False, "manual": True, "steps": steps, "file": str(target),
                "hint": f"本机没有 systemd。请常驻运行：{cmd}"}

    unit = SYSTEMD_DIR / feishu_relay.SERVICE_NAME
    try:
        unit.write_text(feishu_relay.render_service(), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "steps": steps,
                "error": f"写入 {unit} 失败（需要 root）：{exc}"}
    steps.append(_run(["systemctl", "daemon-reload"]))
    steps.append(_run(["systemctl", "enable", "--now", feishu_relay.SERVICE_NAME]))
    from . import feishu_setup
    st = feishu_setup._relay_status()
    return {"ok": bool(st.get("running")), "steps": steps, "status": st}
