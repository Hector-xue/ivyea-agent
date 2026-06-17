"""降级链 provider —— 主脑失败(重试后仍挂/无额度)自动切备用模型。

按序尝试 [主, 备1, 备2…]。流式时：若某个 provider 在**尚未吐出任何内容**前失败，
切下一个；若已吐过内容才失败，不能安全降级，直接抛(避免重复输出)。
"""
from __future__ import annotations

from typing import Callable, Optional

from .base import LLMError, LLMProvider


class ChainProvider(LLMProvider):
    name = "chain"

    def __init__(self, members: list, narrate: Optional[Callable[[str], None]] = None):
        # members: [(provider, label), …]，至少一个
        self.members = members
        self.model = members[0][0].model
        self.api_key = members[0][0].api_key
        self._narrate = narrate or (lambda s: None)

    def _note(self, label: str, err) -> None:
        self._narrate(f"\033[33m[降级] {label} 失败（{str(err)[:80]}），切下一个模型…\033[0m")

    def complete(self, system, user, **kw):
        last = None
        for prov, label in self.members:
            try:
                return prov.complete(system, user, **kw)
            except LLMError as e:
                last = e
                if (prov, label) is not self.members[-1]:
                    self._note(label, e)
        raise LLMError(f"全部模型失败：{last}")

    def chat(self, messages, tools=None, **kw):
        last = None
        for prov, label in self.members:
            try:
                return prov.chat(messages, tools=tools, **kw)
            except LLMError as e:
                last = e
                if (prov, label) is not self.members[-1]:
                    self._note(label, e)
        raise LLMError(f"全部模型失败：{last}")

    def stream_chat(self, messages, tools=None, **kw):
        last = None
        for i, (prov, label) in enumerate(self.members):
            yielded = False
            try:
                for ev in prov.stream_chat(messages, tools=tools, **kw):
                    yielded = True
                    yield ev
                return
            except LLMError as e:
                last = e
                if yielded:           # 已吐内容，不能降级（会重复）
                    raise
                if i < len(self.members) - 1:
                    self._note(label, e)
                    continue
                raise
        if last:
            raise last
