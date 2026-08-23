"""卡片回调处理 —— 与飞书 SDK 解耦的纯逻辑，便于完整测试。

``handle_card_action`` 只吃普通 dict，吐 (是否有回填卡片, 卡片 JSON, 日志文案)。
飞书 SDK 的封装留在 relay.py，这样这段安全逻辑不需要连长连接就能测。
"""
import logging

from . import agent_client
from . import gates

log = logging.getLogger("relay.handlers")

_DEDUP = gates.TokenDedup()

_ACTIONS = ("approve", "deny", "rollback", "detail",
            # 这三个不带 approval_id：批量批准用「被点的那张卡的 message_id」定位，
            # 开写开关是全局动作
            "approve_all", "approve_all_confirm", "operate_on")
_BARE_ACTIONS = ("approve_all", "approve_all_confirm", "operate_on")


def _card(title: str, body: str, template: str = "grey") -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": [{"tag": "markdown", "content": body}],
    }


def parse_value(value) -> tuple:
    """从按钮 value 取 (action, approval_id)。契约与 agent 的 feishu_card 一致。

    非法输入返回空串而不抛异常——回调路径上抛异常会让飞书一直重投。
    """
    if not isinstance(value, dict):
        return "", ""
    action = str(value.get("ivyea_action") or "")
    if action not in _ACTIONS:
        return "", ""
    return action, str(value.get("approval_id") or "")


def handle_card_action(*, value, operator_open_id: str, chat_id: str,
                       token: str = "", message_id: str = "", dedup=None) -> tuple:
    """三道闸 + 转发。返回 (回填卡片 or None, 日志文案)。"""
    dedup = _DEDUP if dedup is None else dedup
    action, approval_id = parse_value(value)
    if not action:
        return None, "忽略：按钮 value 不合契约"
    if action not in _BARE_ACTIONS and not approval_id:
        return None, "忽略：按钮缺 approval_id"

    # 闸 1：发送者白名单。不在名单里静默丢弃——不给探测者任何反馈。
    if not gates.sender_allowed(operator_open_id):
        log.warning("拒绝：%s 不在白名单，动作=%s approval=%s",
                    operator_open_id or "<unknown>", action, approval_id)
        return None, f"拒绝：{operator_open_id or '未知用户'} 不在白名单"

    if not gates.chat_allowed(chat_id):
        log.warning("拒绝：会话 %s 不在白名单", chat_id)
        return None, f"拒绝：会话 {chat_id} 不在白名单"

    # 闸 3：重投去重。幂等返回提示，不再打到 agent。
    if dedup.seen(token):
        log.info("重投忽略：token=%s approval=%s", token[:12], approval_id)
        return _card("⏳ 处理中", "这次点击已经在处理了，请稍候。"), "重投忽略"

    if action in _BARE_ACTIONS:
        payload = {"action": action, "operator_open_id": operator_open_id,
                   "chat_id": chat_id}
        if action.startswith("approve_all"):
            if not message_id:
                return _card("⚠️ 无法定位", "拿不到这张卡片的 ID，请改用单条批准。",
                             "orange"), "缺 message_id"
            payload["message_id"] = message_id
        if action == "operate_on":
            payload["minutes"] = int((value or {}).get("minutes") or 120)
        try:
            code, data = agent_client.action(payload)
        except agent_client.AgentUnavailable as exc:
            return _card("⚠️ 后端不可用", f"点击未生效。\n\n`{exc}`", "red"), "agent 不可用"
        card = data.get("card")
        if card:
            return card, f"HTTP {code} {action}"
        return _card("⚠️ 未执行", data.get("error") or data.get("detail") or "未生效",
                     "orange"), f"HTTP {code} {action}"

    if action == "detail":
        try:
            code, data = agent_client.status(approval_id)
        except agent_client.AgentUnavailable as exc:
            return _card("⚠️ 后端不可用", f"取详情失败：{exc}", "red"), "agent 不可用"
        if code != 200:
            return _card("⚠️ 未找到", "该审批项不存在或已被清理。", "red"), "详情 404"
        lines = [f"**{data.get('preview', '')}**",
                 f"状态：{data.get('state')}",
                 f"审计号：{data.get('audit_id') or '—'}"]
        ev = data.get("evidence") or {}
        if ev:
            lines.append("**证据**")
            lines += [f"- {k}：{v}" for k, v in list(ev.items())[:8]]
        return _card("🔎 详情", "\n".join(lines), "blue"), "已返回详情"

    try:
        if action == "rollback":
            code, data = agent_client.rollback(approval_id, operator_open_id, chat_id)
        else:
            code, data = agent_client.resolve(approval_id, action,
                                              operator_open_id, chat_id)
    except agent_client.AgentUnavailable as exc:
        # 绝不静默吞掉点击：用户必须知道没生效。
        log.error("agent 不可用：%s", exc)
        return _card("⚠️ 后端不可用",
                     f"点击未生效，agent 服务无法访问。\n\n`{exc}`", "red"), "agent 不可用"

    card = data.get("card")
    if card:
        return card, f"HTTP {code} state={data.get('state', '')}"
    if code == 409:
        return _card("ℹ️ 无需处理", data.get("detail") or "该操作已处理过。"), f"409 {data.get('reason')}"
    if not data.get("ok"):
        return _card("⚠️ 未执行", data.get("detail") or "操作未生效。", "orange"), \
            f"未执行 {data.get('reason', '')}"
    return _card("✅ 已处理", data.get("detail") or "完成。", "green"), f"HTTP {code}"
