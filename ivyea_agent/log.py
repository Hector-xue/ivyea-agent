"""极轻量 debug 日志：仅在 IVYEA_DEBUG 环境变量或 config debug=true 时落盘，否则 no-op。

给那些过去 `except ...: pass` 静默吞错的地方一个去处——平时零开销/零噪声，
排障时 `IVYEA_DEBUG=1 ivyea ...` 即可在 ~/.ivyea/logs/agent.log 看到真实失败。
"""
from __future__ import annotations

import os
import time


def enabled() -> bool:
    if os.environ.get("IVYEA_DEBUG"):
        return True
    try:
        from . import config
        return bool(config.get_setting("debug", False))
    except Exception:
        return False


def dbg(scope: str, msg: str) -> None:
    """记一条 debug 日志（仅在开启时）。绝不抛出，绝不影响主流程。"""
    if not enabled():
        return
    try:
        from . import config
        d = config.IVYEA_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{scope}] {msg}\n"
        with (d / "agent.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
