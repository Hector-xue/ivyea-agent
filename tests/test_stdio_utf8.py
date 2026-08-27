"""serve 的开场白不能被一个 ✓ 崩掉。

回归的是真事：IvyeaOps 起 serve 时把 stdout 接到日志文件/NUL，Windows 于是按
系统代码页（中文机器 = GBK）编码，`print("  ✓ …")` 抛 UnicodeEncodeError，守护
进程当场退出，用户那边只看得到 "All connection attempts failed"。

所以这里**故意把 stdout 换成一个 strict 的 GBK 流**——不加护栏它必崩。
"""
from __future__ import annotations

import io
import sys


class _DummyServer:
    server_address = ("127.0.0.1", 8765)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt   # 让 run() 走完开场白就收摊

    def server_close(self) -> None:
        pass


def _gbk_stdout(monkeypatch) -> io.BytesIO:
    from ivyea_agent import stdio_utf8

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="gbk", errors="strict"))
    monkeypatch.setattr(stdio_utf8, "_done", False)   # 每个用例都从"还没修"开始
    return raw


def test_force_utf8_rescues_a_gbk_stream(monkeypatch):
    from ivyea_agent import stdio_utf8

    raw = _gbk_stdout(monkeypatch)
    stdio_utf8.force_utf8()
    print("  ✓ 巡检节拍器")
    sys.stdout.flush()
    assert "✓ 巡检节拍器" in raw.getvalue().decode("utf-8")


def test_serve_banner_survives_a_gbk_stdout(monkeypatch):
    from ivyea_agent import serve_workers, service

    monkeypatch.setattr(service, "make_server", lambda *a, **k: _DummyServer())
    monkeypatch.setattr(serve_workers, "start_all",
                        lambda *a, **k: {"scheduler": {"started": True, "reason": "已在本进程内启动"}})
    raw = _gbk_stdout(monkeypatch)

    service.run()   # 不加 force_utf8 时，这一行抛 UnicodeEncodeError

    sys.stdout.flush()
    out = raw.getvalue().decode("utf-8")
    assert "✓ scheduler: 已在本进程内启动" in out
    assert "listening on http://127.0.0.1:8765" in out
