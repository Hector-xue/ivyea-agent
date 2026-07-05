"""全屏 TUI 聊天骨架（P0）。"""
from __future__ import annotations

import io
import re


from ivyea_agent import chat_tui


def _plain(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)


def test_tui_default_on_and_opt_out(monkeypatch):
    import types
    monkeypatch.setattr(chat_tui.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(chat_tui.sys, "stdout", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.delenv("IVYEA_TUI", raising=False)
    monkeypatch.delenv("IVYEA_LIVE", raising=False)
    assert chat_tui.tui_enabled() is True                 # 默认全屏 alt-screen TUI（输入框钉死）
    for off in ("0", "false", "Off", "NO"):
        monkeypatch.setenv("IVYEA_TUI", off)
        assert chat_tui.tui_enabled() is False            # IVYEA_TUI=0 → 滚动区/行式
    monkeypatch.delenv("IVYEA_TUI", raising=False)
    monkeypatch.setenv("IVYEA_LIVE", "1")                 # IVYEA_LIVE=1（要滚动区）→ 不进 alt-screen
    assert chat_tui.tui_enabled() is False


def test_tui_disabled_when_not_tty(monkeypatch):
    import types
    monkeypatch.setattr(chat_tui.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(chat_tui.sys, "stdout", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.delenv("IVYEA_TUI", raising=False)        # 默认开，但非 TTY 也回退
    assert chat_tui.tui_enabled() is False


def _run_headless(keys: str, status="  ivyea · GPT-5.5 · dry-run "):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.data_structures import Size
    import prompt_toolkit.application as ptkapp

    buf = io.StringIO()
    with create_pipe_input() as pinp:
        orig = ptkapp.Application

        def factory(*a, **k):
            k.setdefault("input", pinp)
            k.setdefault("output", Vt100_Output(buf, lambda: Size(rows=30, columns=100), term="xterm-256color"))
            return orig(*a, **k)

        ptkapp.Application = factory
        try:
            pinp.send_text(keys)
            rc = chat_tui.run(lambda: status, [("/model", "x"), ("/exit", "退出")])
        finally:
            ptkapp.Application = orig
    return rc, _plain(buf.getvalue())


def test_skeleton_clean_exit_and_status():
    rc, vis = _run_headless("/exit\r")
    assert rc == 0                                   # /exit 干净退出
    assert "GPT-5.5" in vis                          # footer 显示 status


def test_question_echoed_inline():
    # 正常 Q/A 流：问题内联回显进 transcript（不再置顶固定头部），滚动到某回答即见其问题
    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=lambda *a, **k: {"text": ""}, render_markdown=lambda s: s)
    tui._start_turn("排查任务")
    assert any("排查任务" in _plain(b) for b in tui.blocks)


def test_skeleton_ctrl_c_exits():
    rc, _ = _run_headless("\x03")                    # Ctrl-C 退出
    assert rc == 0


# ---- P1 核心闭环 ----
def _wait_idle(tui, timeout=2.0):
    import time
    end = time.time() + timeout
    while time.time() < end and tui.running:
        time.sleep(0.02)


def test_turn_streams_and_interleaves_tool_lines():
    def fake_turn(line, render, narrate):
        render("你好"); narrate("⏺ 读取文件"); render("，世界")
        return {"text": "你好，世界", "usage": {}, "blocked": False}

    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=fake_turn, render_markdown=lambda s: s)
    tui._start_turn("改 greet.py")
    _wait_idle(tui)
    assert tui.running is False
    assert tui.instruction == "改 greet.py"          # sticky 头部指令
    assert tui.live is None                           # 流式已定稿
    plain = _plain("\n".join(tui.blocks))
    assert tui.instruction == "改 greet.py"           # 指令在头部（不再 transcript 回显）
    assert "你好" in plain and "，世界" in plain      # 两段流式文本
    # Claude 式交错：文本 → 工具行 → 继续文本
    assert plain.index("你好") < plain.index("⏺ 读取文件") < plain.index("，世界")


def test_scrollback_stream_does_not_commit_code_at_internal_blank_line(monkeypatch):
    printed = []
    monkeypatch.setattr(chat_tui, "_ptprint", printed.append)
    emitter = chat_tui._ScrollEmitter(lambda text: "MD<" + text + ">")
    emitter.text("```python\ndef hello():\n\n")
    assert printed == []
    emitter.text("    return 1\n```\n\n")
    assert len(printed) == 1
    assert "def hello():\n\n    return 1" in printed[0]
    assert printed[0].count("MD<") == 1


def test_turn_error_surfaced():
    def boom(line, render, narrate, cancel_check=None):
        raise RuntimeError("炸了")
    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=boom, render_markdown=lambda s: s)
    tui._start_turn("x")
    _wait_idle(tui)
    assert tui.running is False
    assert "炸了" in _plain("\n".join(tui.blocks))


# ---- P2 中断 + 排队 ----
def test_run_turn_stream_cancel_check_raises():
    from ivyea_agent import agent_loop, agent_tools

    class _P:
        def stream_chat(self, *a, **k):
            yield {"type": "text", "text": "x"}

    ctx = agent_tools.ToolContext(session_id="s")
    ctx.turn_id = "t"
    import pytest as _pt
    with _pt.raises(KeyboardInterrupt):
        agent_loop.run_turn_stream(_P(), ctx, [{"role": "user", "content": "hi"}],
                                   cancel_check=lambda: True)


def test_tui_interrupt_preserves_session():
    import time
    def slow(line, render, narrate, cancel_check=None):
        for _ in range(200):
            if cancel_check and cancel_check():
                raise KeyboardInterrupt
            time.sleep(0.005)
        return {"text": "done"}
    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=slow, render_markdown=lambda s: s)
    tui._start_turn("长任务")
    time.sleep(0.05)
    tui.cancel_requested = True
    _wait_idle(tui)
    assert tui.running is False
    assert "已中断" in _plain("\n".join(tui.blocks))


def test_tui_queue_auto_continues():
    def quick(line, render, narrate, cancel_check=None):
        render(f"答:{line}")
        return {"text": line}
    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=quick, render_markdown=lambda s: s)
    tui.running = True
    tui.queued = ["第二条"]
    tui._finish({"text": "第一条 done"})    # 结束首轮 → 自动跑排队的下一条
    _wait_idle(tui)
    assert tui.instruction == "第二条"                       # 队列已自动继续到第二条
    assert "答:第二条" in _plain("\n".join(tui.blocks))      # 第二条的回复已渲染


# ---- P3 审批 marshal 到 TUI ----
def test_approval_marshaled_from_tool_thread():
    import threading
    import time
    from ivyea_agent import tui as tui_mod

    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=lambda *a, **k: {"text": ""},
                           render_markdown=lambda s: s)
    tui_mod.set_active_selector(tui._approve)
    res = {}

    def worker():
        res["r"] = tui_mod.select("需要确认写操作", "编辑 demo.py：替换 1 处",
                                  [("approve", "批准本次"), ("deny", "拒绝"), ("abort", "全部停止")])

    th = threading.Thread(target=worker)
    th.start()
    for _ in range(200):
        if tui.pending:
            break
        time.sleep(0.005)
    try:
        assert tui.pending is not None
        panel = _plain("\n".join(tui._approval_lines()))
        assert "批准本次" in panel and "编辑 demo.py" in panel   # 选项 + diff 预览
        tui._confirm_approval(0)                                  # 选“批准本次”
        th.join(timeout=1)
        assert res.get("r") == "approve"
        assert tui.pending is None
    finally:
        tui_mod.set_active_selector(None)


# ---- P4 输入对齐 ----
def test_handle_submit_routes():
    from ivyea_agent.cli import _plan_mode_intent
    called = {}
    tui = chat_tui.ChatTUI(
        status_fn=lambda: "s", turn_fn=lambda *a, **k: {"text": ""}, render_markdown=lambda s: s,
        slash_commands=[("/exit", "退出"), ("/plan", "计划"), ("/model", "模型")],
        plan_intent_fn=_plan_mode_intent,
        set_plan_mode=lambda on: ("已进入计划模式" if on else "已退出计划模式"),
        cycle_mode=lambda: "自动接受编辑",
        slash_handlers={"/model": lambda line: called.__setitem__("model", line)},
    )
    assert tui._handle_submit("/exit") == "exit"
    tui.blocks = ["x"]
    assert tui._handle_submit("/clear") == "handled" and tui.blocks == []
    assert tui._handle_submit("进入计划模式") == "handled"
    assert "计划模式" in _plain("\n".join(tui.blocks))
    assert tui._handle_submit("/model gpt") == "handled"       # 有 handler → 执行
    assert called.get("model") == "/model gpt"
    assert tui._handle_submit("/unknown") == "turn"            # 无 handler 的 slash → 交给 turn_fn 展开/提示
    assert tui._handle_submit("帮我分析广告") == "turn"        # 普通 → 跑一轮


def test_completer_slash():
    from prompt_toolkit.document import Document
    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=lambda *a, **k: {"text": ""},
                           render_markdown=lambda s: s, slash_commands=[("/exit", "退出"), ("/plan", "计划")])
    cs = list(tui._completer().get_completions(Document("/e"), None))
    assert [c.text for c in cs] == ["/exit"]


def test_approval_ctrl_c_picks_last_abort():
    import threading
    import time
    from ivyea_agent import tui as tui_mod

    tui = chat_tui.ChatTUI(status_fn=lambda: "s", turn_fn=lambda *a, **k: {"text": ""},
                           render_markdown=lambda s: s)
    tui_mod.set_active_selector(tui._approve)
    res = {}

    def worker():
        res["r"] = tui_mod.select("t", "b", [("approve", "a"), ("deny", "d"), ("abort", "停止")])

    th = threading.Thread(target=worker)
    th.start()
    for _ in range(200):
        if tui.pending:
            break
        time.sleep(0.005)
    try:
        tui._confirm_approval(len(tui.pending["options"]) - 1)   # 模拟 Ctrl-C 选最后
        th.join(timeout=1)
        assert res.get("r") == "abort"
    finally:
        tui_mod.set_active_selector(None)
