"""飞书接入的配置面 —— 一处填写，四条链路共用。

``feishu_client`` 只管**传输**（换 token、发卡片、改卡片）。这里管的是**配置**：
凭据落在哪、白名单是谁、还差哪一步、IvyeaOps 那张向导该点亮到第几步。
两件事的消费方和变更频率都不一样，混在一个模块里，改一处必然连坐另一处。

四条链路共用同一份 ``~/.ivyea`` 配置：

1. 巡检告警   store_health → notify.feishu_app → 交互卡片
2. 审批闭环   卡片按钮 → relay → agent → 领星写入
3. 飞书对话   relay → /v1/chat
4. 连接自检   本模块的 status/probe

**故意不合并进来的第五条**：IvyeaOps 自己的 CPU/服务器告警（``scripts/cpu_alert.py``
读 ``hub_settings.json`` 的 ``alert_*``）。它是看门狗——agent 挂了、8765 不通了，
它还得能把消息发出去。让看门狗去依赖被看的那个进程的配置，等于在最需要报警的
时候没有报警。所以两边**各存一份凭据、由 IvyeaOps 界面同时写**，而不是一边读另一边。

配置的**生效值**永远以 ``feishu_client._creds()``（.env 优先）为准，本模块的
``status()`` 报的也是生效值。凭据同时写 .env 和 settings.json 两处、且只在
``configure()`` 里一起写，是为了让"界面改了但没生效"这种事在结构上不可能发生。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

from . import config

#: relay 的 systemd 单元名（长连接接收端，卡片按钮和飞书对话都靠它）。
#: 随包发布后规范名是 ``ivyea-feishu-relay.service``；旧名是本机手工部署时用的，
#: **必须继续认**，否则升级完会把一个跑得好好的服务显示成"未安装"。
RELAY_SERVICE = "ivyea-feishu-relay.service"
LEGACY_RELAY_SERVICES = ("feishu-ivyea-relay.service",)

ENV_APP_ID = "IVYEA_FEISHU_APP_ID"
ENV_APP_SECRET = "IVYEA_FEISHU_APP_SECRET"

#: 字符串型配置 → settings.json 里的键名
_STR_FIELDS = {
    "domain": "feishu_domain",
    "chat_id": "feishu_default_chat_id",
    "webhook_url": "feishu_webhook_url",
}
#: 列表型配置（白名单）
_LIST_FIELDS = {
    "allowed_senders": "feishu_allowed_senders",
    "allowed_chats": "feishu_allowed_chats",
}

#: 巡检任务的规范名。IvyeaOps 界面写的就是这几条，
#: 手工注册的同类任务在 configure_patrol 里会被一起收编（见那里的说明）。
PATROL_JOBS = {
    "l1": ("patrol-l1", "store_l1"),
    "l2": ("patrol-l2", "store_l2"),
    "daily": ("patrol-daily", "store_daily"),
    "weekly": ("patrol-weekly", "store_weekly"),
    "monthly": ("patrol-monthly", "store_monthly"),
}
PATROL_TASKS = {task for _, task in PATROL_JOBS.values()}

#: 界面上每一档的说明。措辞在这里，不在前端 —— 改一次两边都对。
PATROL_LABELS = {
    "l1": ("实时层 L1", "库存断货 / 活动被暂停 / 预算被外部改动 / listing 上下架"),
    "l2": ("日内层 L2", "当日花费突增 / 曝光归零 / 点击暴涨零转化"),
    "daily": ("每日早报", "昨日指标 + 环比 + 待你决定的建议（带按钮）"),
    "weekly": ("每周周报", "本周 vs 上周 + 本周批了/执行了什么（只回顾，无按钮）"),
    "monthly": ("每月月报", "本月 vs 上月，同样只回顾"),
}


# ── 读 ──────────────────────────────────────────────────────────────────────
def _as_list(raw: Any) -> list[str]:
    """白名单既可能是 JSON 数组，也可能是界面上一行逗号/换行分隔的文本。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").replace(" ", ",").split(",")
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def gates() -> dict[str, list[str]]:
    """relay 的两道白名单闸。relay 每次校验都来读，改完不用重启。"""
    s = config.load_settings()
    return {
        "allowed_senders": _as_list(s.get("feishu_allowed_senders")),
        "allowed_chats": _as_list(s.get("feishu_allowed_chats")),
    }


def _mask(value: str, keep: int = 6) -> str:
    if not value:
        return ""
    return value if len(value) <= keep + 2 else f"{value[:keep]}…{value[-2:]}"


def _relay_status() -> dict[str, Any]:
    """relay 是否在跑。**查不出来时报 unknown，不报 stopped。**

    "没查到"和"没在跑"是两回事：Windows 上压根没有 systemd，把它显示成
    "已停止"会让人去修一个根本不存在的服务。
    """
    from . import feishu_relay

    sdk = feishu_relay.sdk_available()
    if os.name == "nt" or not shutil.which("systemctl"):
        return {"state": "unknown", "running": None, "sdk": sdk,
                "detail": "本机没有 systemd，无法自动判定；"
                          "常驻运行 `python -m ivyea_agent.feishu_relay` 即可"}

    seen: list[tuple[str, str]] = []
    for name in (RELAY_SERVICE, *LEGACY_RELAY_SERVICES):
        try:
            proc = subprocess.run(["systemctl", "is-active", name],
                                  capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
            return {"state": "unknown", "running": None, "sdk": sdk,
                    "detail": f"查询失败：{exc}"}
        state = (proc.stdout or proc.stderr or "").strip() or "unknown"
        seen.append((name, state))
        if state == "active":
            return {"state": state, "running": True, "sdk": sdk,
                    "detail": f"{name} 运行中"}

    name, state = seen[0]
    if any(st == "inactive" for _n, st in seen):
        inactive = next(n for n, st in seen if st == "inactive")
        return {"state": "inactive", "running": False, "sdk": sdk,
                "detail": f"{inactive} 已安装但没在跑：systemctl start {inactive}"}
    # is-active 对"没装过这个单元"也回 inactive/unknown，措辞上不要武断。
    # 给的是**能直接敲的命令**——只说"未安装"等于让人自己去猜怎么装。
    how = ("先装 SDK：pip install \"ivyea-agent[feishu]\"，再 `ivyea relay install`"
           if not sdk else "`ivyea relay install`（写 systemd 单元并启动）")
    return {"state": state, "running": False, "sdk": sdk,
            "detail": f"接收端未运行 —— {how}"}


def patrol_defaults() -> dict[str, Any]:
    """各档的默认间隔与说明。**界面从这里取默认值，不在前端再写一份**——
    两处各写一份时，实际生效的永远是小的那个，而且没人记得改另一处。"""
    from . import schedule

    out = {}
    for key, (_name, task) in PATROL_JOBS.items():
        label, desc = PATROL_LABELS.get(key, (key, ""))
        out[key] = {"task": task, "label": label, "desc": desc,
                    "every_minutes": schedule.PATROL_DEFAULT_MINUTES.get(task, 1440.0)}
    return out


def patrol_status() -> dict[str, Any]:
    """已注册的店铺巡检任务。**只注册不等于会跑**，所以连触发器一起报。"""
    from . import schedule

    jobs = [j for j in schedule.load().get("jobs", [])
            if str(j.get("task") or "") in PATROL_TASKS]
    rows = []
    for j in jobs:
        args = j.get("args") or {}
        rows.append({
            "name": j.get("name"),
            "task": j.get("task"),
            "enabled": bool(j.get("enabled", True)),
            "every_minutes": float(j["every_minutes"]) if j.get("every_minutes")
            else float(j.get("every_hours") or 24.0) * 60.0,
            "channel": str(args.get("channel") or "stdout"),
            "notify": bool(args.get("notify")),
            "scope": ("all" if str(args.get("sids") or "").lower() == "all"
                      else ("sids" if args.get("sids") else "sid")),
            "sids": _as_list(args.get("sids")) if str(args.get("sids") or "").lower() != "all"
            else [],
            "sid": str(args.get("sid") or ""),
            "last_run": float(j.get("last_run") or 0),
        })
    pushing = [r for r in rows if r["enabled"] and r["notify"] and r["channel"].startswith("feishu")]
    return {
        "jobs": rows,
        "defaults": patrol_defaults(),
        "any_enabled": any(r["enabled"] for r in rows),
        "pushing_to_feishu": len(pushing),
        "timer": _timer_status(),
    }


def _timer_status() -> dict[str, Any]:
    """巡检任务的触发器。注册了任务却没装 timer = 以为在跑其实没跑。"""
    if os.name == "nt" or not shutil.which("systemctl"):
        return {"state": "unknown", "running": None,
                "detail": "本机没有 systemd；请用计划任务定期执行 `ivyea schedule run-due`"}
    try:
        proc = subprocess.run(["systemctl", "is-active", "ivyea-schedule.timer"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        return {"state": "unknown", "running": None, "detail": f"查询失败：{exc}"}
    state = (proc.stdout or proc.stderr or "").strip() or "unknown"
    running = state == "active"
    return {"state": state, "running": running,
            "detail": ("ivyea-schedule.timer 运行中" if running
                       else "ivyea-schedule.timer 未启用，注册的巡检任务不会被触发"
                            "（deploy/systemd/ivyea-schedule.timer）")}


def status(*, probe: bool = False) -> dict[str, Any]:
    """配置全景。``probe=True`` 会真的去飞书换一次 token 并列会话。

    默认不联网：这个接口是配置页每次打开都要调的，不该每次都打飞书。
    """
    from . import feishu_client

    app_id, secret = feishu_client._creds()
    s = config.load_settings()
    g = gates()
    chat_id = str(s.get("feishu_default_chat_id") or "")
    webhook = str(s.get("feishu_webhook_url") or s.get("notify_webhook_url") or "")
    relay = _relay_status()
    patrol = patrol_status()

    probe_result: dict[str, Any] = {"ran": False}
    if probe:
        probe_result = {"ran": True, **feishu_client.verify()}

    app_ready = bool(app_id and secret)
    out: dict[str, Any] = {
        "ok": True,
        "app": {
            "app_id": app_id,
            "app_id_masked": _mask(app_id),
            "configured": app_ready,
            "secret_configured": bool(secret),
            "domain": str(s.get("feishu_domain") or "feishu"),
            "source": "env" if os.environ.get(ENV_APP_ID) else ("settings" if app_id else ""),
        },
        "chat": {"chat_id": chat_id, "configured": bool(chat_id)},
        "webhook": {"configured": bool(webhook), "url_masked": _mask(webhook, 40)},
        "gates": g,
        "relay": relay,
        "patrol": patrol,
        "probe": probe_result,
        "last_test_at": float(s.get("feishu_last_test_at") or 0),
    }
    out["channels"] = _channels(app_ready, chat_id, webhook, g, relay, patrol)
    out["steps"] = _steps(out)
    return out


def _channels(app_ready: bool, chat_id: str, webhook: str, g: dict,
              relay: dict, patrol: dict) -> dict[str, Any]:
    """四条链路各自还差什么。

    分开报是因为它们的门槛**不一样**，而这正是最容易被误解的地方：
    群机器人 webhook 能发文本告警，但它没有回调通道，永远点不了审批按钮。
    只配了 webhook 的人会以为"飞书已经配好了"，然后奇怪为什么卡片没有按钮。
    """
    def entry(ready: bool, blockers: list[str], note: str = "") -> dict[str, Any]:
        return {"ready": ready, "blockers": blockers, "note": note}

    text_blockers = [] if (app_ready or webhook) else ["没有应用凭据，也没有群机器人 webhook"]
    cards_blockers = []
    if not app_ready:
        cards_blockers.append("缺应用凭据（App ID / App Secret）")
    if not chat_id:
        cards_blockers.append("没有默认会话 chat_id，卡片不知道发给谁")

    approval_blockers = list(cards_blockers)
    if relay.get("running") is False:
        approval_blockers.append("relay 未运行，按钮点了没人接")
    if not g["allowed_senders"]:
        approval_blockers.append("审批白名单为空 —— 按安全默认，此时没有人能点按钮")

    chat_blockers = []
    if not app_ready:
        chat_blockers.append("缺应用凭据")
    if relay.get("running") is False:
        chat_blockers.append("relay 未运行")

    patrol_blockers = []
    if not patrol["any_enabled"]:
        patrol_blockers.append("没有启用任何巡检任务")
    elif not patrol["pushing_to_feishu"]:
        patrol_blockers.append("巡检任务在跑，但没有一条推到飞书（channel 不是 feishu_app）")
    if patrol["timer"].get("running") is False:
        patrol_blockers.append("ivyea-schedule.timer 未启用，任务不会被触发")
    patrol_blockers += cards_blockers

    return {
        "text_alert": entry(not text_blockers, text_blockers,
                            "纯文本告警：应用身份优先，发不出去时退回群机器人 webhook"),
        "cards": entry(not cards_blockers, cards_blockers,
                       "交互卡片：只有应用身份能发，webhook 机器人发不了"),
        "approval": entry(not approval_blockers, approval_blockers,
                          "点按钮改领星：需要卡片 + relay 长连接 + 白名单"),
        "chat": entry(not chat_blockers, chat_blockers,
                      "在飞书里直接和 agent 对话（默认只读）"),
        "patrol_push": entry(not patrol_blockers, patrol_blockers,
                             "店铺巡检把异常主动推成卡片"),
    }


def _steps(state: dict[str, Any]) -> list[dict[str, Any]]:
    """向导步骤。顺序 = 真实的配置顺序，每步的 done 都由**可核实的状态**决定，
    不由"用户点过下一步"决定。"""
    app = state["app"]
    probe = state["probe"]
    gates_ = state["gates"]
    steps = [
        {
            "key": "app",
            "title": "创建自建应用，填 App ID / App Secret",
            "done": bool(app["configured"]),
            "detail": app["app_id_masked"] or "未填写",
            "hint": "飞书开放平台 → 开发者后台 → 创建企业自建应用，凭据在「凭证与基础信息」。",
        },
        {
            "key": "permission",
            "title": "开权限并发布版本",
            "done": bool(probe.get("ok")),
            "detail": ("已连通，机器人在 %d 个会话里" % int(probe.get("chat_count") or 0)
                       if probe.get("ok") else
                       (str(probe.get("error")) if probe.get("ran") else "未验证")),
            "hint": "需要 im:message、im:message:send_as_bot、im:chat:readonly；"
                    "改完权限要发布版本并等管理员通过，否则调用会报 99991672。",
        },
        {
            "key": "chat",
            "title": "选一个接收会话（群）",
            "done": bool(state["chat"]["configured"]),
            "detail": state["chat"]["chat_id"] or "未选择",
            "hint": "先把机器人拉进群，再在这里选。列不出来通常就是没拉进群。",
        },
        {
            "key": "whitelist",
            "title": "指定谁能点审批按钮",
            "done": bool(gates_["allowed_senders"]),
            "detail": (f"{len(gates_['allowed_senders'])} 人" if gates_["allowed_senders"]
                       else "空 —— 按安全默认，没有人能点"),
            "hint": "留空不是「所有人都能点」，是「所有人都不能点」。改钱的权限不设默认放行。",
        },
        {
            "key": "relay",
            "title": "启动长连接接收端 relay",
            "done": bool(state["relay"].get("running")),
            "detail": str(state["relay"].get("detail") or ""),
            "hint": "只发告警可以不装；要点按钮、要在飞书里对话就必须装。"
                    "装法：pip install \"ivyea-agent[feishu]\" 后执行 `ivyea relay install`。"
                    "它走长连接，不需要对公网开放任何端口。",
        },
        {
            "key": "patrol",
            "title": "打开店铺巡检并推到飞书",
            "done": bool(state["patrol"]["pushing_to_feishu"]),
            "detail": (f"{state['patrol']['pushing_to_feishu']} 条任务在推"
                       if state["patrol"]["pushing_to_feishu"] else "未开启"),
            "hint": "L1 每小时 / L2 每 12 小时 / 早报每天一张汇总卡，另有周报与月报。",
        },
        {
            "key": "test",
            "title": "发一条测试消息确认真能收到",
            "done": bool(state["last_test_at"]),
            "detail": (time.strftime("%Y-%m-%d %H:%M", time.localtime(state["last_test_at"]))
                       if state["last_test_at"] else "未测试"),
            "hint": "配置页保存不等于发得出去。以真收到为准。",
        },
    ]
    return steps


# ── 写 ──────────────────────────────────────────────────────────────────────
def configure(payload: dict[str, Any]) -> dict[str, Any]:
    """写入飞书配置。IvyeaOps 系统配置页保存时下推这一份。

    规则（与 ``/v1/config/vision`` 的"绝不主动清除"同源）：

    - **键不在 payload 里 = 不动**。ops 只推它管的那几项，不该顺手清掉 CLI 用户
      自己配的东西。
    - **字符串键值为空 = 不动**，除非在 ``clear`` 里点名。界面上一个没填的输入框
      会老老实实传空串过来，把它当"清除"就会出现"打开配置页什么都没干、
      保存一下飞书就瞎了"。
    - **列表键给了空列表 = 真清空**。白名单要能删到一个人不剩，这是安全动作，
      必须有确定路径。

    凭据 app_id/secret 同时写 ``.env``（生效源）和 ``settings.json``（展示源），
    一起写才不会出现"界面显示新的、实际用的是旧的"。
    """
    clear = {str(x).strip() for x in (payload.get("clear") or [])}
    changed: list[str] = []
    settings = config.load_settings()

    app_id = str(payload.get("app_id") or "").strip()
    secret = str(payload.get("app_secret") or "").strip()
    if app_id or "app_id" in clear:
        _write_env(ENV_APP_ID, app_id)
        settings["feishu_app_id"] = app_id
        changed.append("app_id")
    if secret or "app_secret" in clear:
        _write_env(ENV_APP_SECRET, secret)
        changed.append("app_secret")
    if app_id or secret:
        # 换了应用，旧 token 立刻作废：留着它，下一条消息会用旧应用的身份发出去
        _drop_token_cache()

    for field, key in _STR_FIELDS.items():
        if field not in payload:
            continue
        val = str(payload.get(field) or "").strip()
        if not val and field not in clear:
            continue
        settings[key] = val
        changed.append(field)

    for field, key in _LIST_FIELDS.items():
        if field not in payload:
            continue
        settings[key] = _as_list(payload.get(field))
        changed.append(field)

    config.save_settings(settings)
    return {"ok": True, "changed": changed, "status": status()}


def _write_env(name: str, value: str) -> None:
    """写 ``~/.ivyea/.env`` 并同步当前进程的 os.environ。

    只写文件是不够的：``config.load_env()`` 明确「不覆盖已存在的环境变量」，
    serve 这个常驻进程早就把旧值读进内存了，不同步就会出现
    「保存成功、界面显示新值、发出去的还是旧应用」。
    """
    config.set_env_key(name, value)
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)


def _drop_token_cache() -> None:
    from . import feishu_client
    try:
        feishu_client._TOKEN_FILE.unlink()
    except OSError:
        pass


def send_test(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """发一条真实测试卡片。**成功才记 last_test_at**。"""
    from . import feishu_card, feishu_client

    payload = payload or {}
    chat_id = str(payload.get("chat_id") or "").strip() or feishu_client.default_chat_id()
    if not feishu_client.is_configured():
        return {"ok": False, "error": "未配置应用凭据"}
    if not chat_id:
        return {"ok": False, "error": "没有目标会话：先选一个群，或在这次调用里带上 chat_id"}
    text = str(payload.get("text") or "").strip() or (
        "这是 IvyeaOps 发来的飞书连通性测试。\n"
        "收到这条，说明凭据、权限、目标会话三样都对了。\n"
        "巡检告警和审批卡片也会发到这里。")
    try:
        message_id = feishu_client.send_card(chat_id, feishu_card.build_text_card("飞书连通性测试", text))
    except feishu_client.FeishuError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code, "chat_id": chat_id}
    config.set_setting("feishu_last_test_at", time.time())
    return {"ok": True, "message_id": message_id, "chat_id": chat_id}


def list_chats() -> dict[str, Any]:
    """机器人所在的群。

    **空列表是正常结果，不是故障。** 实测（2026-08-23，本机真应用）：机器人明明在
    群里、卡片也发得进去，``im/v1/chats`` 照样返回 ``code=0`` + 空 items——
    那个接口只列"应用主动创建/被授权可管理"的群。所以界面必须同时支持手工粘
    ``oc_`` 开头的 chat_id，不能把选群做成唯一入口。
    """
    from . import feishu_client
    if not feishu_client.is_configured():
        return {"ok": False, "error": "未配置应用凭据", "chats": []}
    try:
        chats = feishu_client.list_chats()
    except feishu_client.FeishuError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code, "chats": []}
    return {"ok": True, "chats": chats,
            "note": ("" if chats else
                     "接口没列出任何群。这不代表没配好——飞书只列应用可管理的群。"
                     "把群里「设置 → 群机器人」的会话 ID 直接粘进来即可。")}


def list_members(chat_id: str = "") -> dict[str, Any]:
    from . import feishu_client
    if not feishu_client.is_configured():
        return {"ok": False, "error": "未配置应用凭据", "members": []}
    try:
        return {"ok": True, "members": feishu_client.list_chat_members(chat_id)}
    except feishu_client.FeishuError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code, "members": []}


def configure_patrol(payload: dict[str, Any]) -> dict[str, Any]:
    """按界面上的选择重建三条巡检任务。

    **会先收编已存在的同类任务**：手工注册过 ``l1-1863`` 之类的单店任务时，
    如果只是再加一条全店任务，下一轮 timer 会让同一个店被巡两遍、飞书里出现
    两张几乎一样的卡。所以这里把所有 ``store_*`` 任务清掉再按界面重建，
    并把清掉的名字如实回报，不做无声替换。
    """
    from . import schedule

    scope = str(payload.get("scope") or "all").strip().lower()
    sids = _as_list(payload.get("sids"))
    exclude = _as_list(payload.get("exclude_sids"))
    channel = str(payload.get("channel") or "feishu_app").strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    notify = bool(payload.get("notify", True))

    if scope == "sids" and not sids:
        return {"ok": False, "error": "选了「指定店铺」但一个 sid 都没给"}

    base_args: dict[str, Any] = {"notify": notify, "channel": channel}
    if chat_id:
        base_args["chat_id"] = chat_id
    if scope == "all":
        base_args["sids"] = "all"
    else:
        base_args["sids"] = sids
    if exclude:
        base_args["exclude_sids"] = exclude

    data = schedule.load()
    replaced = [j["name"] for j in data.get("jobs", [])
                if str(j.get("task") or "") in PATROL_TASKS]
    for name in replaced:
        schedule.remove_job(name)

    created = []
    defaults = patrol_defaults()
    for key, (name, task) in PATROL_JOBS.items():
        spec = payload.get(key) or {}
        if not spec.get("enabled"):
            continue
        # 没给间隔就用这一档的默认值，而不是硬编码的 24 小时——
        # 周报默认 7 天，被悄悄改成 1 天就成了"每天一份周报"。
        if spec.get("every_minutes"):
            minutes = float(spec["every_minutes"])
        elif spec.get("every_hours"):
            minutes = float(spec["every_hours"]) * 60.0
        else:
            minutes = float(defaults[key]["every_minutes"])
        job = schedule.set_job(name, task, args=dict(base_args), every_minutes=minutes)
        created.append(job["name"])

    return {"ok": True, "created": created,
            "replaced": [n for n in replaced if n not in created],
            "patrol": patrol_status()}
