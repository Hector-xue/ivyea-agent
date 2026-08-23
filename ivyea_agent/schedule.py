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
                 # 店铺业务巡检（各层节奏见 store_health 的分层说明）
                 "store_l1", "store_l2", "store_daily",
                 # 回顾型报告：只汇总不派活，**不创建审批项**（见 patrol_push.push_period）
                 "store_weekly", "store_monthly",
                 "approvals_expire"}

#: 各巡检任务的默认间隔（分钟）。**唯一真源**——CLI、IvyeaOps 界面、文档都读这里，
#: 别在前端再写一份默认值（写两份时实际生效的永远是小的那个）。
#: 回顾型报告 → 窗口天数与措辞
PERIOD_TASKS = {"store_weekly": (7, "周报"), "store_monthly": (30, "月报")}

PATROL_DEFAULT_MINUTES = {
    "store_l1": 60.0,        # 快照差分：库存/活动配置/listing 状态
    "store_l2": 720.0,       # 日内累计差分（12 小时）
    "store_daily": 1440.0,   # 早报
    "store_weekly": 10080.0,  # 周报（7 天）
    "store_monthly": 43200.0,  # 月报（30 天）
}


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

    ``every_minutes`` 是为分钟级巡检加的：L1 默认每小时、可调到 20 分钟，
    写成 ``every_hours=0.333``
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
    if task in ("store_l1", "store_l2", "store_daily",
                "store_weekly", "store_monthly"):
        return _run_store_task(task, args)

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
        # **每跑完一个就落盘**，不能攒到最后一起写。多店铺巡检把单次 run-due 从
        # 十几秒拉长到几分钟，中途被 systemd 超时杀掉/机器重启的概率不再可忽略；
        # 只在末尾 save 的话，已经跑完的任务的 last_run 会一起丢，下一轮全部重跑——
        # 对早报就是同一张卡再推一遍。
        save(data)
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
        every = (f"{job['every_minutes']:g}m" if job.get("every_minutes")
                 else f"{float(job.get('every_hours', 24)):g}h")
        lines.append(
            f"{job['name']:<18} task={job['task']:<16} every={every:<6} "
            f"enabled={job.get('enabled', True)} last={last}"
        )
    return "\n".join(lines)


def _run_store_task(task: str, args: dict[str, Any]) -> tuple[bool, str]:
    """店铺巡检任务的入口：解析目标店铺，逐店执行，聚合结果。

    为什么是「一个任务巡检多个店」而不是「每店注册一个任务」：11 个店 × 三层
    ＝ 33 条 job，每条都要手工维护 sid 与 store_name，加第 12 个店就得记得补 3 条。
    目标解析交给 ``stores.resolve_targets``，schedule.json 里只留一句 ``sids: "all"``。

    **逐店隔离是硬要求**：第 5 个店抛异常不能吃掉第 6~11 个店。所以每店一个
    try/except，失败的店变成一行错误进聚合输出，而不是让整批巡检崩掉。
    """
    from . import stores

    try:
        targets = stores.resolve_targets(args)
    except Exception as exc:                            # noqa: BLE001
        return False, f"店铺清单不可用，无法确定巡检目标：{exc}\n"
    if not targets:
        return False, "店铺巡检任务缺少 sid / sids 参数（或目标被 exclude_sids 全部排除）"

    # 周报/月报没有"单店旧格式"要保，一店多店走同一条装配。
    if task in PERIOD_TASKS:
        return _run_period_report(task, args, targets)

    # 单店时保持原样返回，输出与旧版逐字一致——旧的 job、测试、IvyeaOps 的
    # 解析都依赖这个格式，不能因为支持了多店就顺手改掉单店的输出。
    if len(targets) == 1:
        return _run_store_task_one(task, args, targets[0])

    if task == "store_daily" and str(args.get("channel") or "") == "feishu_app":
        return _run_store_daily_multi(args, targets)

    if task in PERIOD_TASKS:
        return _run_period_report(task, args, targets)

    oks: list[bool] = []
    chunks: list[str] = []
    for store in targets:
        ok, text = _run_store_task_one(task, args, store)
        oks.append(ok)
        chunks.append(f"—— {store.get('name')}（sid {store.get('sid')}）——\n{text}")
    n_ok = sum(1 for o in oks if o)
    header = f"== 多店铺巡检 {task}：{len(targets)} 个店，成功 {n_ok}，失败 {len(targets) - n_ok} ==\n"
    return all(oks), header + "\n".join(chunks)


def _run_store_daily_multi(args: dict[str, Any],
                           targets: list[dict[str, Any]]) -> tuple[bool, str]:
    """多店早报：先把所有店跑完，最后合成一张卡发出去。

    先跑完再发，是为了让卡片能按"最坏的店"决定标题颜色、并把跨店异常按严重度
    排在一起。代价是发卡时间等于所有店的巡检耗时之和——早报每天一次，可以接受；
    L1 那种小时级的不能这么干，所以只有早报和周报/月报走这条路。
    """
    import datetime

    from . import patrol_push, reliability, store_health

    date = datetime.date.today().isoformat()
    rows: list[dict[str, Any]] = []
    failed: list[str] = []

    for store in targets:
        sid = store.get("sid")
        name = str(store.get("name") or f"sid {sid}")
        try:
            result = store_health.check_l3(
                sid, days=int(args.get("days") or 7),
                include_optimizer=bool(args.get("include_optimizer", True)))
            summary = store_health.daily_summary(
                sid, days=int(args.get("summary_days") or 1))
            result.gaps.extend(summary["gaps"])
        except Exception as exc:                        # noqa: BLE001 —— 逐店隔离
            failed.append(f"{name}（sid {sid}）：{type(exc).__name__}: {exc}")
            continue

        # 连续失败计数仍按店记，一个坏店不该污染其它店的健康度
        health_key = f"patrol.store_daily.{sid}"
        if result.gaps:
            reliability.record_failure(health_key, "；".join(result.gaps)[:300])
        else:
            reliability.record_success(health_key)
        rows.append({"name": name, "sid": sid, "result": result,
                     "metrics_lines": summary["lines"]})

    if not rows:
        return False, "所有店铺的早报都取数失败：\n" + "\n".join(f"- {f}" for f in failed)

    pushed = patrol_push.push_daily_multi(
        rows, date=date, chat_id=str(args.get("chat_id") or ""),
        report_url=str(args.get("report_url") or ""))

    lines = [f"== 多店铺早报 {date}：{len(rows)} 个店 =="]
    for r in rows:
        lines.append(f"  {r['name']}：异常 {len(r['result'].findings)} 条，"
                     f"缺口 {len(r['result'].gaps)} 条")
    for f in failed:
        lines.append(f"  ! 取数失败：{f}")
    if not pushed.get("ok"):
        lines.append(f"  早报推送失败：{pushed.get('error')}")
        return False, "\n".join(lines) + "\n"
    lines.append(f"  已推送：message_id={pushed['message_id']} "
                 f"待决定 {len(pushed['approvals'])} 条")
    return not failed, "\n".join(lines) + "\n"


def _run_period_report(task: str, args: dict[str, Any],
                       targets: list[dict[str, Any]]) -> tuple[bool, str]:
    """周报 / 月报：把窗口拉长跑一遍 L3，加上本期的审批动态，合成一张卡。

    与早报的三点不同：
    1. **不创建审批项**——同一条建议在早报里已经给过按钮，重复创建会让同一个
       目标挂两条待办（见 patrol_push.push_period）。
    2. 窗口是 7 / 30 天，环比对照的是上一个同长度窗口。
    3. 无论一店还是多店都是一张卡：周报本来就是拿来"整体看一眼"的。
    """
    import datetime

    from . import approvals, patrol_push, store_health

    days, label = PERIOD_TASKS[task]
    rows: list[dict[str, Any]] = []
    failed: list[str] = []

    for store in targets:
        sid = store.get("sid")
        name = str(store.get("name") or f"sid {sid}")
        try:
            result = store_health.check_l3(sid, days=days, include_optimizer=False)
            summary = store_health.period_summary(sid, days=days)
        except Exception as exc:                    # noqa: BLE001 —— 逐店隔离
            failed.append(f"{name}（sid {sid}）：{type(exc).__name__}: {exc}")
            continue
        rows.append({"name": name, "sid": sid, "findings": result.sorted_findings(),
                     "gaps": list(result.gaps) + list(summary["gaps"]),
                     "metrics_lines": summary["lines"]})

    if not rows:
        return False, f"{label}全部取数失败：\n" + "\n".join(f"- {f}" for f in failed)

    today = datetime.date.today()
    window = f"{(today - datetime.timedelta(days=days)).isoformat()}~" \
             f"{(today - datetime.timedelta(days=1)).isoformat()}"
    act = approvals.activity(time.time() - days * 86400,
                             sid=(targets[0].get("sid") if len(targets) == 1 else ""))

    channel = str(args.get("channel") or "stdout")
    text_lines = [f"== 店铺{label} {window}：{len(rows)} 个店 =="]
    for r in rows:
        text_lines.append(f"  {r['name']}：问题 {len(r['findings'])} 条，"
                          f"缺口 {len(r['gaps'])} 条")
        text_lines.extend(f"    {ln}" for ln in r["metrics_lines"])
    for f in failed:
        text_lines.append(f"  ! 取数失败：{f}")
    text_lines.append(f"  本期动作：新建议 {act['created']} · 已执行 {act['executed']}"
                      f" · 回滚 {act['rolled_back']} · 待处理 {act['pending_now']}")
    text = "\n".join(text_lines) + "\n"

    if not channel.startswith("feishu"):
        return not failed, text

    pushed = patrol_push.push_period(
        rows, period=label, window=window, activity=act,
        chat_id=str(args.get("chat_id") or ""),
        report_url=str(args.get("report_url") or ""))
    if not pushed.get("ok"):
        return False, text + f"  {label}推送失败：{pushed.get('error')}\n"
    return not failed, text + f"  已推送：message_id={pushed['message_id']}\n"


def _run_store_task_one(task: str, args: dict[str, Any],
                        store: dict[str, Any]) -> tuple[bool, str]:
    """单个店铺的巡检。异常在这里收口成失败结果，绝不外抛。"""
    sid = store.get("sid")
    store_name = str(store.get("name") or args.get("store_name") or f"sid {sid}")
    try:
        return _store_task_body(task, args, sid, store_name)
    except Exception as exc:                            # noqa: BLE001 —— 隔离，见上
        return False, f"店铺 {store_name}（sid {sid}）巡检异常：{type(exc).__name__}: {exc}\n"


def _store_task_body(task: str, args: dict[str, Any], sid: Any,
                     store_name: str) -> tuple[bool, str]:
    from . import store_health
    if task == "store_l1":
        result = store_health.check_l1(sid)
    elif task == "store_l2":
        result = store_health.check_l2(sid)
    else:
        result = store_health.check_l3(sid, days=int(args.get("days") or 7),
                                       include_optimizer=bool(
                                           args.get("include_optimizer", True)))
    # 早报走卡片装配（方案 §5.5）：指标 + 异常 + 待决定 + 数据缺口
    if task == "store_daily" and str(args.get("channel") or "") == "feishu_app":
        import datetime

        from . import patrol_push
        summary = store_health.daily_summary(sid, days=int(args.get("summary_days") or 1))
        result.gaps.extend(summary["gaps"])
        pushed = patrol_push.push_daily(
            result,
            date=datetime.date.today().isoformat(),
            store_name=store_name,
            metrics_lines=summary["lines"],
            chat_id=str(args.get("chat_id") or ""),
            report_url=str(args.get("report_url") or ""))
        text = store_health.render(result)
        if not pushed.get("ok"):
            return False, f"{text}早报推送失败：{pushed.get('error')}\n"
        return True, (f"{text}早报已推送：message_id={pushed['message_id']} "
                      f"待决定 {len(pushed['approvals'])} 条\n")

    # 数据源健康：只有**连续**失败才告警。巡检按小时级跑，
    # 偶发一次超时是常态，累计计数迟早触发，会变成狼来了（方案 §8.2 / §8.8）。
    from . import reliability
    health_key = f"patrol.{task}.{sid}"
    gap_threshold = 2 if task == "store_daily" else 3
    if result.gaps:
        n = reliability.record_failure(health_key, "；".join(result.gaps)[:300])
        if reliability.should_alert(health_key, gap_threshold):
            from . import feishu_card
            notify.send_alert(
                "数据源连续取数失败：\n" + "\n".join(f"- {g}" for g in result.gaps),
                card=feishu_card.build_text_card(
                    f"🚨 数据源异常（连续 {n} 次）· {store_name}",
                    f"**{store_name}** 的 **{task}** 连续 {n} 次取不到数据，"
                    f"巡检形同虚设。\n\n"
                    + "\n".join(f"- {g}" for g in result.gaps)
                    + "\n\n恢复后会自动清零，不再重复轰炸。",
                    template="red"),
                chat_id=str(args.get("chat_id") or ""), title="数据源异常")
    else:
        reliability.record_success(health_key)

    text = store_health.render(result)
    if not args.get("notify"):
        return True, text

    channel = str(args.get("channel") or "stdout")
    chat_id = str(args.get("chat_id") or "")

    # ── 异常告警：发现即推，不等早报；但同一条问题只在"变成异常的那一刻"报 ──
    # 之前这里走的是 notify.send 纯文本，既没有批准按钮（要动手得等第二天早报），
    # 又每轮重推一次（一个持续一周没处理的断货 = 168 张一模一样的卡，
    # 人的反应是把群静音，然后真出事的那张也看不见了）。
    # 早报不参与节流：它是**汇总**，用户要的就是每天确认一眼，
    # 拿"这条昨天报过了"去删它的内容，早报就残了。节流只管即时告警那条路。
    if task == "store_daily":
        r = notify.send(text, title=str(args.get("title") or f"店铺巡检 {result.layer}"),
                        channel=channel, webhook_url=str(args.get("webhook_url") or ""))
        if not r.get("ok"):
            return False, f"{text}{notify.render_result(r)}\n"
        return True, text

    from . import alert_state

    tri = alert_state.triage(sid, result.layer, result.findings,
                             clean=not result.gaps)
    lines = [text]
    if tri.ongoing:
        lines.append(f"（{len(tri.ongoing)} 条持续中的异常已静默，不重复推送）\n")

    if channel == "feishu_app":
        return _push_alerts(result, tri, args, sid, store_name, chat_id, lines)

    # 非飞书通道（stdout / webhook）：纯文本照旧，但同样只推新增与恢复
    if tri.should_push or result.gaps:
        body = text
        if tri.resolved:
            body += _resolved_text(tri.resolved)
        r = notify.send(body, title=str(args.get("title") or f"店铺巡检 {result.layer}"),
                        channel=channel, webhook_url=str(args.get("webhook_url") or ""))
        if not r.get("ok"):
            return False, f"{text}{notify.render_result(r)}\n"
    return True, "".join(lines)


def _resolved_text(resolved: list[dict[str, Any]]) -> str:
    """恢复通知的正文。**必须发**——只报坏消息的系统，人无法判断问题有没有解决。"""
    rows = "\n".join(f"- {r.get('message') or r.get('code')}" for r in resolved[:10])
    more = f"\n…另有 {len(resolved) - 10} 条" if len(resolved) > 10 else ""
    return f"\n✅ 已恢复 {len(resolved)} 条：\n{rows}{more}\n"


def _push_alerts(result: Any, tri: Any, args: dict[str, Any], sid: Any,
                 store_name: str, chat_id: str, lines: list[str]) -> tuple[bool, str]:
    """把本轮**新增**的异常推成带按钮的卡片，恢复的另发一条。

    只拿 ``tri.fresh`` 去建审批项：持续中的异常若每轮都建一条 approval，
    同一个目标会攒出一堆待办，批哪条都说不清改的是哪一次。
    """
    from . import feishu_card, patrol_push

    ok = True
    if tri.fresh:
        # 只把新增的那几条装进卡片；result 的其余字段（sid/layer/gaps）照用
        subset = _result_subset(result, tri.fresh)
        pushed = patrol_push.push_result(subset, chat_id=chat_id, store_name=store_name,
                                         channel="feishu_app")
        if pushed.get("ok"):
            lines.append(f"已推送异常卡片：message_id={pushed['message_id']} "
                         f"新增 {len(tri.fresh)} 条，待决定 {len(pushed['approvals'])} 条\n")
        else:
            ok = False
            lines.append(f"异常卡片推送失败：{pushed.get('error')}\n")

    if tri.resolved:
        body = "\n".join(f"- {r.get('message') or r.get('code')}"
                          for r in tri.resolved[:10])
        sent = notify.send_alert(
            _resolved_text(tri.resolved),
            card=feishu_card.build_text_card(
                f"✅ 已恢复 {len(tri.resolved)} 条 · {store_name}", body, template="green"),
            chat_id=chat_id, title="异常已恢复")
        if not sent.get("ok"):
            ok = False
            lines.append(f"恢复通知推送失败：{sent.get('error')}\n")

    return ok, "".join(lines)


def _result_subset(result: Any, findings: list[Any]) -> Any:
    """同一个 CheckResult 的浅拷贝，只换掉 findings。

    不直接改 result.findings：调用方后面还要用完整结果渲染正文与统计。
    """
    import copy

    subset = copy.copy(result)
    subset.findings = list(findings)
    return subset
