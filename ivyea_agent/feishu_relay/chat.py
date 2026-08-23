"""飞书对话入口 —— 消息事件 → agent /v1/chat。

**不起 `ivyea chat -p` 子进程**：serve 已由 systemd 托管、会话落盘、工具预算 200 步。
子进程方案每次冷启动，还会重现"撞工具上限"的老问题。

会话映射 chat_id → session_id 落盘：relay 重启后对话上下文不该断。
消息 id 去重：飞书会重投，重投不能变成第二次跑 agent（既费钱又可能重复动作）。
"""
import json
import logging
import os
import threading

import httpx

from . import config

log = logging.getLogger("relay.chat")

_LOCK = threading.Lock()

HELP = (
    "**Ivyea Agent**\n"
    "- 直接发消息即可对话（默认只读，不会改广告）\n"
    "- `/reset` 清空本会话上下文\n"
    "- `/threshold` 查看巡检阈值；`/threshold <键> <值>` 修改；`/threshold reset [键]` 复位\n"
    "- `/operate [分钟]` 开启领星写开关（默认 120 分钟，带自动失效）\n"
    "- `/help` 显示这段说明\n"
    "\n改广告这类写操作走告警卡片上的「批准执行」按钮，那条路有幅度闸和回滚。"
)


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)          # 原子替换，避免写一半被读到


def get_session(chat_id: str):
    return _load(config.SESSIONS_FILE, {}).get(chat_id)


def set_session(chat_id: str, session_id: str) -> None:
    with _LOCK:
        data = _load(config.SESSIONS_FILE, {})
        data[chat_id] = session_id
        _save(config.SESSIONS_FILE, data)


def reset_session(chat_id: str) -> bool:
    with _LOCK:
        data = _load(config.SESSIONS_FILE, {})
        existed = data.pop(chat_id, None) is not None
        _save(config.SESSIONS_FILE, data)
    return existed


def seen_message(message_id: str) -> bool:
    """飞书重投去重。返回 True 表示这条已经处理过。"""
    if not message_id:
        return False
    with _LOCK:
        ids = _load(config.SEEN_FILE, [])
        if message_id in ids:
            return True
        ids.append(message_id)
        if len(ids) > config.SEEN_MAX:
            ids = ids[-config.SEEN_MAX:]
        _save(config.SEEN_FILE, ids)
    return False


def extract_text(msg_type: str, content: str) -> str:
    """从飞书消息体里取纯文本。非文本消息返回空串（本期只处理文本）。"""
    try:
        body = json.loads(content or "{}")
    except json.JSONDecodeError:
        return ""
    if msg_type == "text":
        return str(body.get("text") or "").strip()
    if msg_type == "post":
        out = []
        for row in (body.get("content") or []):
            for el in row or []:
                if el.get("tag") == "text":
                    out.append(str(el.get("text") or ""))
        return "\n".join(out).strip()
    return ""


def strip_mentions(text: str) -> str:
    """去掉 @机器人 留下的占位符，否则会被当成正文喂给模型。"""
    import re
    return re.sub(r"@_user_\d+", "", text or "").strip()


def run_turn(chat_id: str, text: str) -> str:
    """跑一轮对话，返回给用户的文本。异常都转成可读文案，不往上抛。"""
    body = {
        "message": text,
        "plan_mode": config.CHAT_PLAN_MODE,
        "source": "feishu-relay",
    }
    session_id = get_session(chat_id)
    if session_id:
        body["session_id"] = session_id

    url = config.AGENT_URL.rstrip("/") + "/v1/chat"
    try:
        r = httpx.post(url, json=body, timeout=config.CHAT_TIMEOUT)
    except httpx.HTTPError as exc:
        log.error("agent 不可用：%s", exc)
        return f"⚠️ agent 服务无法访问，本轮未执行。\n`{exc}`"
    try:
        data = r.json()
    except ValueError:
        return f"⚠️ agent 返回不可解析（HTTP {r.status_code}）"

    if not data.get("ok"):
        return f"⚠️ {data.get('error') or '执行失败'}：{data.get('detail') or ''}".strip()

    new_session = str(data.get("session_id") or "")
    if new_session and new_session != session_id:
        set_session(chat_id, new_session)
    return str(data.get("text") or "（本轮没有文本输出）")


def handle_message(*, chat_id: str, sender_open_id: str, message_id: str,
                   msg_type: str, content: str) -> str:
    """返回要回复的文本；返回空串表示不回复。"""
    from . import gates

    if not gates.sender_allowed(sender_open_id):
        log.warning("拒绝对话：%s 不在白名单", sender_open_id or "<unknown>")
        return ""
    if not gates.chat_allowed(chat_id):
        log.warning("拒绝对话：会话 %s 不在白名单", chat_id)
        return ""
    if seen_message(message_id):
        log.info("重投忽略：message_id=%s", message_id)
        return ""

    text = strip_mentions(extract_text(msg_type, content))
    if not text:
        return "本期只处理文本消息。"

    if config.CHAT_PREFIX:
        if not text.startswith(config.CHAT_PREFIX):
            return ""
        text = text[len(config.CHAT_PREFIX):].strip()

    low = text.lower()
    if low in ("/help", "help", "帮助"):
        return HELP
    if low in ("/reset", "reset", "清空"):
        return "已清空本会话上下文。" if reset_session(chat_id) else "本会话本来就是空的。"
    if low.startswith("/threshold"):
        return _threshold_cmd(text.split()[1:])
    if low.startswith("/operate"):
        parts = text.split()
        minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 120
        return _operate_cmd(minutes, sender_open_id)

    return run_turn(chat_id, text)


def _call_action(payload: dict):
    url = config.AGENT_URL.rstrip("/") + "/v1/feishu/action"
    try:
        r = httpx.post(url, json=payload, timeout=30)
        return r.status_code, r.json()
    except (httpx.HTTPError, ValueError) as exc:
        return 0, {"ok": False, "error": str(exc)}


def _threshold_cmd(args) -> str:
    if not args:
        code, data = _call_action({"action": "threshold_list"})
        if not data.get("ok"):
            return f"⚠️ 取阈值失败：{data.get('error')}"
        lines = ["**巡检阈值**（带 ✏️ 的是你改过的）", ""]
        for row in data["thresholds"]:
            mark = "✏️ " if row["overridden"] else ""
            lines.append(f"- {mark}`{row['key']}` = {row['current']}"
                         + ("" if not row["overridden"] else f"（默认 {row['default']}）"))
        lines.append("")
        lines.append("改：`/threshold ads.acos_breach.factor 1.8`")
        return "\n".join(lines)

    if args[0].lower() == "reset":
        code, data = _call_action({"action": "threshold_reset",
                                   "key": args[1] if len(args) > 1 else ""})
        return f"已复位 {data.get('reset', 0)} 项阈值。" if data.get("ok") \
            else f"⚠️ {data.get('error')}"

    if len(args) < 2:
        return "用法：`/threshold <键> <值>`，或 `/threshold` 查看全部。"
    code, data = _call_action({"action": "threshold_set", "key": args[0],
                               "value": args[1]})
    if data.get("ok"):
        return f"已更新：`{data['key']}` = {data['value']}（立即生效，下次巡检就用新值）"
    return f"⚠️ {data.get('error') or '修改失败'}"


def _operate_cmd(minutes: int, operator: str) -> str:
    code, data = _call_action({"action": "operate_on", "minutes": minutes,
                               "operator_open_id": operator})
    if data.get("ok"):
        return f"🔓 {data.get('detail')}"
    return f"⚠️ {data.get('error') or '开启失败'}"
