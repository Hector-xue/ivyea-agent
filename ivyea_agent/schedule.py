"""Simple local schedule registry.

This is intentionally not a daemon. Cron, systemd timers, IvyeaOps, or a user can
call ``ivyea schedule run-due`` periodically.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import alerts, config, evals, knowledge_quality, knowledge_sync, notify, weekly_review

SCHEDULE_FILE = config.IVYEA_DIR / "schedule.json"
ALLOWED_TASKS = {"alert", "weekly", "eval", "knowledge_quality", "knowledge_sync",
                 # 店铺业务巡检（三层各自的节奏，见 store_health 的分层说明）
                 "store_l1", "store_l2", "store_daily", "approvals_expire"}


def _empty() -> dict[str, Any]:
    return {"jobs": []}


def load() -> dict[str, Any]:
    if not SCHEDULE_FILE.exists():
        return _empty()
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    data.setdefault("jobs", [])
    return data


def save(data: dict[str, Any]) -> None:
    config.ensure_dirs()
    SCHEDULE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_job(name: str, task: str, every_hours: float = 24.0,
            args: dict[str, Any] | None = None,
            every_minutes: float | None = None) -> dict[str, Any]:
    """注册/覆盖一个任务。

    ``every_minutes`` 是为分钟级巡检加的：L1 每 20 分钟，写成 ``every_hours=0.333``
    既不直观又会因四舍五入漂移。传了分钟就以分钟为准，同时回写等价的
    ``every_hours`` 保持老读取方（IvyeaOps / 旧配置）兼容。
    """
    if task not in ALLOWED_TASKS:
        raise ValueError(f"未知任务 {task}，可用：{', '.join(sorted(ALLOWED_TASKS))}")
    data = load()
    jobs = [j for j in data["jobs"] if j.get("name") != name]
    job = {
        "name": name,
        "task": task,
        "every_hours": (float(every_minutes) / 60.0 if every_minutes
                        else float(every_hours or 24.0)),
        "args": args or {},
        "last_run": 0.0,
        "enabled": True,
    }
    if every_minutes:
        job["every_minutes"] = float(every_minutes)
    jobs.append(job)
    data["jobs"] = sorted(jobs, key=lambda j: j["name"])
    save(data)
    return job


def remove_job(name: str) -> bool:
    data = load()
    before = len(data["jobs"])
    data["jobs"] = [j for j in data["jobs"] if j.get("name") != name]
    save(data)
    return len(data["jobs"]) != before


def due_jobs(now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    rows = []
    for job in load()["jobs"]:
        if not job.get("enabled", True):
            continue
        if job.get("every_minutes"):
            every = max(1.0, float(job["every_minutes"])) * 60
        else:
            every = max(0.01, float(job.get("every_hours") or 24.0)) * 3600
        if now - float(job.get("last_run") or 0) >= every:
            rows.append(job)
    return rows


def run_task(task: str, args: dict[str, Any] | None = None) -> tuple[bool, str]:
    args = args or {}
    if task == "alert":
        text = alerts.render(alerts.check(limit=int(args.get("limit") or 500)))
        if args.get("notify"):
            result = notify.send(
                text,
                title=str(args.get("title") or "Ivyea Alerts"),
                channel=str(args.get("channel") or "stdout"),
                webhook_url=str(args.get("webhook_url") or ""),
            )
            if not result.get("ok"):
                return False, f"{text}\n{notify.render_result(result)}\n"
        return True, text
    if task in ("store_l1", "store_l2", "store_daily"):
        from . import store_health
        sid = args.get("sid")
        if not sid:
            return False, "店铺巡检任务缺少 sid 参数"
        if task == "store_l1":
            result = store_health.check_l1(sid)
        elif task == "store_l2":
            result = store_health.check_l2(sid)
        else:
            result = store_health.check_l3(sid, days=int(args.get("days") or 7),
                                           include_optimizer=bool(
                                               args.get("include_optimizer", True)))
        text = store_health.render(result)
        # 静默规则：无异常且无数据缺口时不推送，避免每 20 分钟刷屏。
        # 早报例外——用户要的就是"每天确认一眼"。
        quiet = (task != "store_daily" and not result.findings and not result.gaps)
        if args.get("notify") and not quiet:
            r = notify.send(text, title=str(args.get("title") or f"店铺巡检 {result.layer}"),
                            channel=str(args.get("channel") or "stdout"),
                            webhook_url=str(args.get("webhook_url") or ""))
            if not r.get("ok"):
                return False, f"{text}{notify.render_result(r)}\n"
        return True, text

    if task == "approvals_expire":
        from . import approvals
        n = approvals.expire_due()
        return True, f"已把 {n} 条超期未处理的审批标记为 expired。\n"

    if task == "weekly":
        return True, weekly_review.render(weekly_review.build(limit=int(args.get("limit") or 200)))
    if task == "eval":
        result = evals.run()
        return bool(result["ok"]), evals.render(result)
    if task == "knowledge_sync":
        result = knowledge_sync.sync(
            force=bool(args.get("force", False)),
            source_ids=[str(v) for v in args.get("source_ids") or []],
        )
        return bool(result["ok"]), knowledge_sync.render_sync(result)
    if task == "knowledge_quality":
        result = knowledge_quality.run()
        return bool(result["ok"]), knowledge_quality.render(result)
    return False, f"未知任务：{task}"


def run_due(now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    data = load()
    jobs = data["jobs"]
    out = []
    for job in due_jobs(now=now):
        ok, text = run_task(job["task"], job.get("args") or {})
        out.append({"job": job["name"], "task": job["task"], "ok": ok, "output": text})
        for stored in jobs:
            if stored.get("name") == job["name"]:
                stored["last_run"] = now
                break
    save(data)
    return out


def render_jobs() -> str:
    jobs = load()["jobs"]
    if not jobs:
        return "（暂无计划任务）"
    lines = []
    for job in jobs:
        last = "-"
        if job.get("last_run"):
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(job["last_run"]))
        lines.append(
            f"{job['name']:<18} task={job['task']:<7} every={job.get('every_hours', 24)}h "
            f"enabled={job.get('enabled', True)} last={last}"
        )
    return "\n".join(lines)
