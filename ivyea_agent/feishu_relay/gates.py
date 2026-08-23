"""relay 侧的三道闸（方案 §6.1 的 1/2/3）。

纯函数 + 一个内存去重表，便于在没有飞书连接的情况下完整测试。
第 2 道（回调会话与发卡会话一致）在 agent 侧按 approval 记录做最终判定——
relay 只负责把 chat_id 如实带过去，不自作主张。
"""
import threading
import time

from . import config


class TokenDedup:
    """闸 3：卡片回调 token 去重。

    飞书在网络抖动时会重投同一次点击。重投若变成第二次执行，就是第二次花钱。
    """

    def __init__(self, ttl: float = None, cap: int = None):
        self._ttl = config.CARD_TOKEN_TTL if ttl is None else ttl
        self._cap = config.CARD_TOKEN_MAX if cap is None else cap
        self._seen = {}
        self._lock = threading.Lock()

    def seen(self, token: str, now: float = None) -> bool:
        """返回 True 表示**这次是重投**，调用方应幂等返回上次结果。"""
        if not token:
            return False
        now = time.time() if now is None else now
        with self._lock:
            self._expire(now)
            if token in self._seen:
                return True
            self._seen[token] = now
            # 修剪放在插入之后：先剪再插会让容量停在 cap+1
            self._trim()
            return False

    def _expire(self, now: float) -> None:
        for k in [k for k, t in self._seen.items() if now - t > self._ttl]:
            self._seen.pop(k, None)

    def _trim(self) -> None:
        if len(self._seen) <= self._cap:
            return
        for k, _ in sorted(self._seen.items(), key=lambda kv: kv[1])[
                :len(self._seen) - self._cap]:
            self._seen.pop(k, None)

    def __len__(self) -> int:
        return len(self._seen)


def sender_allowed(open_id: str, allowed: set = None) -> bool:
    """闸 1：发送者白名单。

    **留空 = 全部拒绝**。空集合当"放行所有"是危险默认：
    忘配一次就等于把改广告预算的权限开给会话里的每一个人。
    """
    # 每次判定都重取：白名单在 IvyeaOps 界面上改完必须立刻生效，
    # 用启动时的快照会让"撤权"要等到下次重启才算数。
    allowed = config.allowed_sender_ids() if allowed is None else allowed
    if not allowed:
        return False
    return bool(open_id) and open_id in allowed


def chat_allowed(chat_id: str, allowed: set = None) -> bool:
    """会话白名单。留空表示不额外限制（最终一致性由 agent 按 approval 判定）。"""
    allowed = config.allowed_chat_ids() if allowed is None else allowed
    if not allowed:
        return True
    return bool(chat_id) and chat_id in allowed
