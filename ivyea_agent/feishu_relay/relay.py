"""feishu-ivyea-relay —— 飞书长连接 → ivyea-agent。

职责刻意很窄：**接住事件、过三道闸、转发给 agent、把结果卡片同步回填**。
所有业务判断（审批状态机、写开关、幅度硬闸、真实写入、回滚）都在 agent 侧，
relay 不做任何决定，也拿不到领星凭据。

为什么走长连接：卡片回调（``card.action.trigger``）与消息事件都能从长连接收到
（已在 hermes 的 gateway/platforms/feishu.py:1611-1641 得到验证），
因此这台机器**不需要向公网开放任何端口**。

启动前必须配 ALLOWED_SENDER_IDS —— 留空时没有人能点按钮（安全默认）。
"""
import logging
import os
import sys

import json
import threading

from . import chat
from . import config
from . import handlers

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("relay")

try:
    import lark_oapi as lark
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.ws import Client as FeishuWSClient
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard, P2CardActionTriggerResponse,
    )
    _SDK = True
except ImportError as exc:                          # pragma: no cover - 依赖缺失时
    from . import SDK_HINT
    log.error("%s（%s）", SDK_HINT, exc)
    _SDK = False


def _attr(obj, *names, default=""):
    """飞书 SDK 各版本字段位置略有差异；逐个尝试，取不到就给默认值。"""
    for name in names:
        cur = obj
        ok = True
        for part in name.split("."):
            cur = getattr(cur, part, None)
            if cur is None:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return default


def on_card_action(data):
    """卡片按钮点击。同步返回替换后的卡片，防重复点击。"""
    operator = str(_attr(data, "event.operator.open_id"))
    chat_id = str(_attr(data, "event.context.open_chat_id"))
    token = str(_attr(data, "event.token", "event.context.preview_token"))
    value = _attr(data, "event.action.value", default={})
    # 批量批准要知道"被点的是哪张卡"——发卡前拿不到 message_id，只能从回调事件里取
    message_id = str(_attr(data, "event.context.open_message_id",
                           "event.open_message_id"))

    card, note = handlers.handle_card_action(
        value=value, operator_open_id=operator, chat_id=chat_id, token=token,
        message_id=message_id)
    log.info("卡片回调 operator=%s chat=%s -> %s", operator or "?", chat_id or "?", note)

    if not _SDK or P2CardActionTriggerResponse is None:
        return None
    resp = P2CardActionTriggerResponse()
    if card and CallBackCard is not None:
        cb = CallBackCard()
        cb.type = "raw"
        cb.data = card
        resp.card = cb
    return resp


_client = None


def _lark_client():
    """回复消息用的 REST 客户端（与长连接分开）。"""
    global _client
    if _client is None:
        _client = (lark.Client.builder()
                   .app_id(config.FEISHU_APP_ID)
                   .app_secret(config.FEISHU_APP_SECRET)
                   .build())
    return _client


def _reply(message_id: str, text: str) -> None:
    """回复原消息。超长按飞书上限切片，逐条回。"""
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    client = _lark_client()
    for i in range(0, max(1, len(text)), 3800):
        chunk = text[i:i + 3800]
        if not chunk:
            break
        req = (ReplyMessageRequest.builder()
               .message_id(message_id)
               .request_body(ReplyMessageRequestBody.builder()
                             .msg_type("text")
                             .content(json.dumps({"text": chunk}, ensure_ascii=False))
                             .build())
               .build())
        resp = client.im.v1.message.reply(req)
        if not resp.success():
            log.error("回复失败 code=%s msg=%s", resp.code, resp.msg)
            return


def on_message(data):
    """消息事件 → agent。**在后台线程里跑**：agent 一轮可能几十秒，
    事件回调里干等会让飞书判定超时并重投。"""
    msg = _attr(data, "event.message")
    if msg is None:
        return
    message_id = str(getattr(msg, "message_id", "") or "")
    chat_id = str(getattr(msg, "chat_id", "") or "")
    msg_type = str(getattr(msg, "message_type", "") or "")
    content = str(getattr(msg, "content", "") or "")
    sender = str(_attr(data, "event.sender.sender_id.open_id"))

    def _run():
        try:
            reply = chat.handle_message(chat_id=chat_id, sender_open_id=sender,
                                        message_id=message_id, msg_type=msg_type,
                                        content=content)
        except Exception as exc:                    # noqa: BLE001
            log.exception("对话处理异常")
            reply = f"⚠️ 处理出错：{exc}"
        if reply:
            _reply(message_id, reply)

    threading.Thread(target=_run, daemon=True).start()


def build_handler():
    return (EventDispatcherHandler
            .builder(config.ENCRYPT_KEY, config.VERIFICATION_TOKEN)
            .register_p2_card_action_trigger(on_card_action)
            .register_p2_im_message_receive_v1(on_message)
            .build())


def main() -> int:
    if not _SDK:
        return 2
    missing = config.missing()
    if missing:
        log.error("配置缺失：%s", "，".join(missing))
        return 2
    for warn in config.warnings():
        log.warning("%s", warn)
    os.makedirs(config.STATE_DIR, exist_ok=True)

    domain = lark.FEISHU_DOMAIN if config.FEISHU_DOMAIN == "feishu" else lark.LARK_DOMAIN
    log.info("启动：白名单 %d 人，会话限制 %s，agent=%s",
             len(config.ALLOWED_SENDER_IDS),
             ("、".join(config.ALLOWED_CHAT_IDS) or "不限"), config.AGENT_URL)

    client = FeishuWSClient(
        config.FEISHU_APP_ID, config.FEISHU_APP_SECRET,
        event_handler=build_handler(), domain=domain,
        log_level=lark.LogLevel.INFO,
    )
    client.start()     # 阻塞；SDK 内部自带断线重连
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
