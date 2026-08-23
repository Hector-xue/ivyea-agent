"""调 ivyea-agent serve 的三个审批端点（方案 §4.2）。"""
import logging

import httpx

from . import config

log = logging.getLogger("relay.agent")


class AgentUnavailable(Exception):
    pass


def _post(path: str, body: dict) -> tuple:
    url = config.AGENT_URL.rstrip("/") + path
    try:
        r = httpx.post(url, json=body, timeout=config.AGENT_TIMEOUT)
    except httpx.HTTPError as exc:
        raise AgentUnavailable(str(exc)) from exc
    try:
        return r.status_code, r.json()
    except ValueError:
        raise AgentUnavailable(f"响应不可解析（HTTP {r.status_code}）")


def resolve(approval_id: str, choice: str, operator_open_id: str, chat_id: str) -> tuple:
    return _post("/v1/feishu/approval/resolve", {
        "approval_id": approval_id, "choice": choice,
        "operator_open_id": operator_open_id, "chat_id": chat_id,
        # 卡片由 relay 同步回填（飞书回调可以带 card 一起返回，比二次调用更稳）
        "update_card": False,
    })


def rollback(approval_id: str, operator_open_id: str, chat_id: str) -> tuple:
    return _post("/v1/feishu/approval/rollback", {
        "approval_id": approval_id, "operator_open_id": operator_open_id,
        "chat_id": chat_id, "update_card": False,
    })


def status(approval_id: str) -> tuple:
    url = config.AGENT_URL.rstrip("/") + f"/v1/feishu/approval/{approval_id}"
    try:
        r = httpx.get(url, timeout=config.AGENT_TIMEOUT)
    except httpx.HTTPError as exc:
        raise AgentUnavailable(str(exc)) from exc
    try:
        return r.status_code, r.json()
    except ValueError:
        raise AgentUnavailable(f"响应不可解析（HTTP {r.status_code}）")


def action(payload: dict) -> tuple:
    """卡片上不绑定单个 approval 的动作（批量批准 / 开写开关 / 调阈值）。"""
    return _post("/v1/feishu/action", payload)
