"""stream-json：`-p --output-format stream-json` 的 NDJSON 事件（对齐 Claude Code）。"""
from __future__ import annotations

import json


class _ToolThenTextProvider:
    """第一步调 recall 工具，第二步纯文本收尾。"""
    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "final", "content": "", "usage": {"prompt_tokens": 50, "completion_tokens": 10},
                   "tool_calls": [{"id": "c1", "name": "recall", "arguments": {"query": "放量"}}]}
        else:
            yield {"type": "text", "text": "结论"}
            yield {"type": "final", "content": "结论", "tool_calls": [],
                   "usage": {"prompt_tokens": 60, "completion_tokens": 8}}


def test_emit_event_sequence(ivyea_home):
    """事件序列：assistant(tool_use) → tool_result(id 配对) → assistant(text)。"""
    from ivyea_agent import agent_loop, agent_tools
    ctx = agent_tools.ToolContext(session_id="sid-sj")
    events = []
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "回忆放量"}]
    out = agent_loop.run_turn_stream(_ToolThenTextProvider(), ctx, msgs,
                                     render=lambda s: None, narrate=lambda s: None,
                                     emit=events.append)
    assert out["text"] == "结论"
    assert [e["type"] for e in events] == ["assistant", "user", "assistant"]
    tool_use = events[0]["message"]["content"][0]
    assert tool_use["type"] == "tool_use" and tool_use["name"] == "recall"
    assert tool_use["input"] == {"query": "放量"}
    tr = events[1]["message"]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == tool_use["id"] == "c1"
    assert isinstance(tr["is_error"], bool)
    final = events[2]["message"]["content"]
    assert final == [{"type": "text", "text": "结论"}]
    assert all(e["session_id"] == "sid-sj" for e in events)


class _ParallelToolsProvider:
    """一步发两个并行安全工具，验证 tool_result 事件按原顺序。"""
    def __init__(self, tmpdir):
        self.calls = 0
        self.tmpdir = tmpdir

    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "final", "content": "", "usage": {},
                   "tool_calls": [
                       {"id": "a1", "name": "list_dir", "arguments": {"path": self.tmpdir}},
                       {"id": "a2", "name": "list_dir", "arguments": {"path": self.tmpdir}}]}
        else:
            yield {"type": "final", "content": "done", "tool_calls": [], "usage": {}}


def test_parallel_tool_results_in_order(ivyea_home, tmp_path):
    from ivyea_agent import agent_loop, agent_tools
    ctx = agent_tools.ToolContext(session_id="sid-par")
    events = []
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "看目录"}]
    agent_loop.run_turn_stream(_ParallelToolsProvider(str(tmp_path)), ctx, msgs,
                               render=lambda s: None, narrate=lambda s: None,
                               emit=events.append)
    trs = [e["message"]["content"][0]["tool_use_id"] for e in events if e["type"] == "user"]
    assert trs == ["a1", "a2"]


def test_emit_exception_does_not_break_turn(ivyea_home):
    """emit 回调抛异常（如消费端断管）不打断主循环。"""
    from ivyea_agent import agent_loop, agent_tools
    ctx = agent_tools.ToolContext()

    def _boom(ev):
        raise BrokenPipeError("consumer gone")

    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}]
    out = agent_loop.run_turn_stream(_ToolThenTextProvider(), ctx, msgs,
                                     render=lambda s: None, narrate=lambda s: None, emit=_boom)
    assert out["text"] == "结论"


def _run_chat_p(monkeypatch, argv, provider):
    """跑 `ivyea chat -p ...`：假 key + 假 provider 链。"""
    from ivyea_agent import providers
    from ivyea_agent.cli import build_parser
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(providers, "build_chain", lambda mcfg, key, narrate=None: provider)
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def test_cli_stream_json_stdout_pure_ndjson(ivyea_home, monkeypatch, capsys):
    """CLI 集成：stdout 每行都是 JSON，首行 init、末行 result，且会话已落盘可续接。"""
    from ivyea_agent import sessions
    rc = _run_chat_p(monkeypatch, ["chat", "-p", "回忆放量", "--output-format", "stream-json"],
                     _ToolThenTextProvider())
    assert rc == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]     # 每行必须可解析（stdout 纯净）
    assert events[0]["type"] == "system" and events[0]["subtype"] == "init"
    assert events[0]["session_id"] and "recall" in events[0]["tools"]
    assert events[-1]["type"] == "result" and events[-1]["is_error"] is False
    assert events[-1]["result"] == "结论"
    assert events[-1]["usage"]["input_tokens"] == 110       # 两步累加
    assert "total_cost_cny" in events[-1]
    assert any(e["type"] == "assistant" for e in events)
    # -p 落盘：session 文件存在且含本轮消息（--resume 可续接）
    data = sessions.load(events[0]["session_id"])
    assert data and any(m.get("role") == "user" for m in data.get("messages") or [])


def test_cli_stream_json_progress_stderr_separate(ivyea_home, monkeypatch, capsys):
    """--progress 时 stderr 有人读进度，stdout 仍全为 JSON。"""
    rc = _run_chat_p(monkeypatch,
                     ["chat", "-p", "回忆放量", "--output-format", "stream-json", "--progress"],
                     _ToolThenTextProvider())
    assert rc == 0
    cap = capsys.readouterr()
    for ln in cap.out.splitlines():
        if ln.strip():
            json.loads(ln)
    assert cap.err.strip()          # 进度进了 stderr


def test_cli_default_text_output_unchanged(ivyea_home, monkeypatch, capsys):
    """回归：不带 --output-format 时仍输出纯文本最终答案。"""
    rc = _run_chat_p(monkeypatch, ["chat", "-p", "回忆放量"], _ToolThenTextProvider())
    assert rc == 0
    out = capsys.readouterr().out
    assert "结论" in out
    assert '"type"' not in out      # 没有 JSON 事件混入
