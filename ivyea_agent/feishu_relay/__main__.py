"""``python -m ivyea_agent.feishu_relay`` —— systemd 单元用的就是这一行。"""
from __future__ import annotations

from .relay import main

if __name__ == "__main__":
    raise SystemExit(main())
