"""serve 进程内的两个常驻工人：巡检节拍器 与 飞书长连接。

**为什么搬进来**：这两样以前各自是一个系统服务，用户得额外装。可 ADR-1 当初把
入站拆出去的两条理由，今天只剩半条站得住：

- "强绑 lark_oapi 会让非中国用户装一堆无用依赖" —— 这条约束的是**依赖**，
  不是**进程**。SDK 已经做成可选依赖（`ivyea-agent[feishu]`），装不装由用户决定，
  与它跑在哪个进程里无关。
- "长连接常驻会污染 CLI 进程模型" —— 这条说的是 `ivyea chat` 那种短命 CLI 进程，
  确实不该挂长连接。但 `serve` 本来就是常驻守护进程（ThreadingHTTPServer，
  已经有心跳等后台线程），在它里面开一个守护线程谈不上污染。

结论是：**入站可以跟着 serve 走，不需要用户再装一个服务。** 独立部署的路子保留
（`ivyea relay install` / systemd timer），给想要进程隔离的人。

**两条硬规矩**（都是"跑两份"会真出事的地方）：

1. **外部服务在跑，就绝不在进程内再跑一份。** 两条飞书长连接意味着同一次按钮
   点击可能被处理两遍（去重表是进程内的，两个进程互相不认）；两个巡检节拍器
   意味着同一份早报推两遍。所以每个工人启动前先探一次系统服务。
2. **一台机器只有一个 serve 该干这活。** 多开 serve 时用文件锁选出一个，
   其余只服务 HTTP。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

from . import config

#: 巡检节拍：每 5 分钟问一次「谁到点了」。与原 systemd timer 同一节奏 ——
#: 具体多久跑一次由 schedule.json 里每个任务自己的间隔决定，这里只是唤醒。
TICK_SECONDS = 300.0

#: 崩了之后的重连退避。飞书长连接偶发断开是常态，SDK 自带重连；
#: 这里兜的是"SDK 自己抛出来了"那种。
_RELAY_BACKOFF = (5.0, 15.0, 60.0, 180.0)

_LOCK_FILE = config.IVYEA_DIR / "serve-workers.lock"
_LOCK_TTL = 900.0

#: 工人状态落盘。**必须跨进程可见** —— 工人活在 serve 进程里，而 `ivyea relay status`、
#: doctor、IvyeaOps 的自检都是**别的进程**在问。只存内存的话，它们会一致地报
#: "没在跑"，然后催用户去装一个其实不需要的服务。
_STATE_FILE = config.IVYEA_DIR / "serve-workers.json"
#: 心跳超过这个岁数就当它没了（serve 被 kill -9 时没机会清理文件）
_STATE_TTL = TICK_SECONDS * 3
#: 续心跳的间隔。必须明显小于 _STATE_TTL，否则自己把自己续成过期。
_HEARTBEAT_SECONDS = TICK_SECONDS / 2

_state: dict[str, Any] = {"scheduler": {}, "relay": {}}
_lock = threading.Lock()


def _setting(name: str, default: str = "auto") -> str:
    """``auto`` = 外部没在跑就自己跑；``off`` = 永不自己跑。"""
    value = config.load_settings().get(f"serve_worker_{name}")
    return str(value or default).strip().lower()


# ── 单实例：多开 serve 时只让一个干活 ────────────────────────────────────────
def _claim() -> bool:
    """抢锁。**过期的锁可以抢** —— 进程被 kill -9 时不会留下清理机会，
    锁若永不过期，重启后这台机器就再也没有节拍器了，而且毫无征兆。"""
    now = time.time()
    try:
        raw = json.loads(_LOCK_FILE.read_text(encoding="utf-8"))
        if raw.get("pid") != os.getpid() and now - float(raw.get("ts") or 0) < _LOCK_TTL:
            return False
    except (OSError, ValueError):
        pass
    try:
        config.ensure_dirs()
        _LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "ts": now}),
                              encoding="utf-8")
    except OSError:
        return False
    return True


def _renew() -> None:
    try:
        _LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}),
                              encoding="utf-8")
    except OSError:
        pass


# ── 巡检节拍器 ──────────────────────────────────────────────────────────────
def _external_timer_running() -> bool:
    from . import host_services

    return bool(host_services.schedule_status().get("running"))


def _scheduler_loop(stop: threading.Event) -> None:
    from . import log as agent_log
    from . import schedule

    while not stop.wait(TICK_SECONDS):
        if not _claim():
            _note("scheduler", running=False, detail="另一个 serve 进程在跑节拍器")
            continue
        _renew()
        try:
            results = schedule.run_due()
        except Exception as exc:                        # noqa: BLE001 —— 绝不能让节拍器死掉
            _note("scheduler", running=True, last_error=f"{type(exc).__name__}: {exc}")
            try:
                agent_log.write("serve-worker", f"run_due 异常：{exc}")
            except Exception:                           # noqa: BLE001
                pass
            continue
        if results:
            _note("scheduler", running=True, last_run=time.time(),
                  last_jobs=[r.get("job") for r in results])
        else:
            _note("scheduler", running=True, last_tick=time.time())


# ── 飞书长连接 ──────────────────────────────────────────────────────────────
def _external_relay_running() -> bool:
    from . import feishu_setup

    return bool(feishu_setup._relay_status().get("running"))


def _relay_loop(stop: threading.Event) -> None:
    attempt = 0
    while not stop.is_set():
        try:
            from .feishu_relay.relay import main as relay_main

            _note("relay", running=True, started_at=time.time())
            relay_main()                                # 阻塞；SDK 内部自带断线重连
            reason = "长连接自行退出"
        except Exception as exc:                        # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"
        if stop.is_set():
            break
        wait = _RELAY_BACKOFF[min(attempt, len(_RELAY_BACKOFF) - 1)]
        attempt += 1
        _note("relay", running=False, last_error=reason, retry_in=wait)
        if stop.wait(wait):
            break
    _note("relay", running=False, detail="已停止")


def _heartbeat_loop(stop: threading.Event, threads: dict[str, threading.Thread]) -> None:
    """替**阻塞中的**工人续心跳。

    长连接线程一旦连上就一直阻塞在 SDK 里，永远不会再调 `_note`。心跳因此变陈旧，
    900 秒后别的进程就把它判成"没在跑"，然后催用户去装一个其实正在跑的服务
    ——线上实测踩到过。所以由这条独立的短循环按"线程还活着吗"来续。
    """
    while not stop.wait(_HEARTBEAT_SECONDS):
        for name, th in threads.items():
            _note(name, running=th.is_alive())


def _note(worker: str, **fields: Any) -> None:
    with _lock:
        _state[worker] = {**_state.get(worker, {}), **fields, "ts": time.time(),
                          "pid": os.getpid()}
        snapshot = {k: dict(v) for k, v in _state.items() if v}
    try:
        config.ensure_dirs()
        _STATE_FILE.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass                                            # 落盘失败不影响工人干活


def _persisted() -> dict[str, Any]:
    """别的进程读到的工人状态。**过期即视为没在跑**。"""
    try:
        raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    return {k: v for k, v in raw.items()
            if isinstance(v, dict) and now - float(v.get("ts") or 0) < _STATE_TTL}


# ── 启动 ────────────────────────────────────────────────────────────────────
def start_all(stop: Optional[threading.Event] = None) -> dict[str, Any]:
    """在 serve 里拉起两个工人。返回每个工人的启动结论（供日志与自检显示）。

    **每一种"没启动"都要说清是哪一种**：关掉了 / 外部服务在跑 / 缺依赖 /
    没配凭据。只报"未运行"的话，用户无从判断该去装什么还是该去配什么。
    """
    stop = stop or threading.Event()
    out: dict[str, Any] = {}
    threads: dict[str, threading.Thread] = {}

    mode = _setting("scheduler")
    if mode == "off":
        out["scheduler"] = {"started": False, "reason": "已在设置里关闭"}
    elif _external_timer_running():
        # 跑两份 = 同一份早报推两遍
        out["scheduler"] = {"started": False, "reason": "系统 timer 已在跑，进程内不重复"}
    else:
        threads["scheduler"] = threading.Thread(
            target=_scheduler_loop, args=(stop,), daemon=True, name="ivyea-scheduler")
        threads["scheduler"].start()
        out["scheduler"] = {"started": True, "running": True,
                            "reason": f"每 {TICK_SECONDS:.0f} 秒唤醒一次"}

    mode = _setting("relay")
    from . import feishu_client, feishu_relay

    if mode == "off":
        out["relay"] = {"started": False, "reason": "已在设置里关闭"}
    elif not feishu_client.is_configured():
        out["relay"] = {"started": False, "reason": "未配置飞书应用凭据"}
    elif not feishu_relay.sdk_available():
        out["relay"] = {"started": False, "reason": feishu_relay.SDK_HINT}
    elif _external_relay_running():
        # 跑两份 = 同一次按钮点击可能被执行两遍（去重表是进程内的）
        out["relay"] = {"started": False, "reason": "独立 relay 服务已在跑，进程内不重复"}
    else:
        threads["relay"] = threading.Thread(
            target=_relay_loop, args=(stop,), daemon=True, name="ivyea-feishu-relay")
        threads["relay"].start()
        out["relay"] = {"started": True, "running": True,
                        "reason": "飞书长连接已在 serve 内建立"}

    if threads:
        threading.Thread(target=_heartbeat_loop, args=(stop, threads), daemon=True,
                         name="ivyea-worker-heartbeat").start()
    for k, v in out.items():
        if not v.get("started"):
            # 清残留：这一轮没启动，就不能让上一轮落盘的 running=True 继续骗人
            _note(k, running=False, **{kk: vv for kk, vv in v.items() if kk != "running"})
        else:
            _note(k, **v)
    return out


def status() -> dict[str, Any]:
    """本进程知道的 + 落盘的。别的进程只有后者，serve 自己两者一致。"""
    out = _persisted()
    with _lock:
        for k, v in _state.items():
            if v:
                out[k] = {**out.get(k, {}), **v}
    return out
