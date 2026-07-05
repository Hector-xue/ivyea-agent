"""思考深度旋钮 + 完成前自验证门禁 + 通用自我批判 + 多轮修复（补齐四缺口）。"""
from __future__ import annotations

import subprocess

from ivyea_agent.agent_tools import ToolContext


def _git_init(root):
    for c in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"]):
        subprocess.run(c, cwd=root, check=True)


# ── Phase 1：思考深度旋钮 ─────────────────────────────────────────────
def test_from_settings_injects_reasoning_effort(monkeypatch):
    from ivyea_agent.providers import base
    monkeypatch.setattr(base.config if hasattr(base, "config") else __import__(
        "ivyea_agent.config", fromlist=["x"]), "get_setting",
        lambda k, d=None: "high" if k == "reasoning_effort" else d)
    p = base.from_settings({"kind": "openai", "model": "deepseek-reasoner",
                            "base_url": "https://api.deepseek.com"}, "sk-x")
    assert p.reasoning_effort == "high"


def test_codex_reasoning_mapping():
    from ivyea_agent.providers.codex_provider import _codex_reasoning
    assert _codex_reasoning("high") == {"summary": "auto", "effort": "high"}
    assert _codex_reasoning("auto") == {"summary": "auto"}            # auto 不指定 effort
    assert _codex_reasoning("off") == {"summary": "auto", "effort": "minimal"}


def test_openai_effort_gated_to_reasoning_models():
    from ivyea_agent.providers.openai_compat import _reasoning_effort_for
    assert _reasoning_effort_for("deepseek-reasoner", "high") == "high"
    assert _reasoning_effort_for("o3-mini", "medium") == "medium"
    assert _reasoning_effort_for("deepseek-chat", "high") is None     # 普通模型不加 → 不 400
    assert _reasoning_effort_for("gpt-4o", "high") is None


def test_gemini_thinking_gated_to_25():
    from ivyea_agent.providers.gemini_provider import _thinking_config
    assert _thinking_config("gemini-2.5-flash", "high") == {"thinkingBudget": 16384}
    assert _thinking_config("gemini-2.5-flash", "off") == {"thinkingBudget": 0}
    assert _thinking_config("gemini-1.5-pro", "high") is None         # 旧模型不加


def test_openai_payload_carries_effort_for_reasoner():
    from ivyea_agent.providers.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider("sk-x", "deepseek-reasoner", "https://api.deepseek.com")
    p.reasoning_effort = "high"
    captured = {}
    p._post = lambda payload, timeout: captured.update(payload) or {
        "choices": [{"message": {"content": "ok", "tool_calls": []}}]}
    p.chat([{"role": "user", "content": "hi"}])
    assert captured.get("reasoning_effort") == "high"


# ── Phase 2：完成前自验证门禁 ─────────────────────────────────────────
def test_verify_gate_blocks_secret(tmp_path):
    from ivyea_agent import verify
    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("x=1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path); subprocess.run(["git", "commit", "-qm", "i"], cwd=tmp_path)
    (tmp_path / "a.py").write_text('api_key = "sk-abcdefgh12345678"\n')
    r = verify.gate(tmp_path, run_tests=False)
    assert r["ok"] is False and r["feedback"].startswith("⚠") and "高危" in r["feedback"]


def test_verify_gate_clean_passes(tmp_path):
    from ivyea_agent import verify
    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("x=1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path); subprocess.run(["git", "commit", "-qm", "i"], cwd=tmp_path)
    assert verify.gate(tmp_path, run_tests=False)["ok"] is True         # 无改动 → 放行


def test_verify_gate_non_git_passes(tmp_path):
    from ivyea_agent import verify
    assert verify.gate(tmp_path, run_tests=False)["ok"] is True


def test_verify_gate_forces_repair_in_loop(tmp_path):
    """核心循环集成：写了带密钥的代码想收尾 → 门禁注回 ⚠ 反馈并逼多跑几轮。"""
    from ivyea_agent import agent_loop
    _git_init(tmp_path)
    (tmp_path / "seed.py").write_text("x=1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path); subprocess.run(["git", "commit", "-qm", "i"], cwd=tmp_path)

    class FP:
        def __init__(self): self.calls = 0
        def chat(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return {"content": "", "tool_calls": [{"id": "1", "name": "write_file", "arguments": {
                    "path": str(tmp_path / "leak.py"), "content": 'api_key = "sk-abcdefgh12345678"\n'}}]}
            return {"content": "改完了", "tool_calls": []}

    ctx = ToolContext(workspace=str(tmp_path))
    ctx.perm.session_allow.add("write_file")
    fp = FP()
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "写一下"}]
    agent_loop.run_turn(fp, ctx, msgs, narrate=lambda s: None)
    injected = [m for m in msgs if m.get("role") == "user" and str(m.get("content", "")).startswith("⚠")]
    assert injected, "门禁应把 ⚠ 反馈注回对话"
    assert fp.calls >= 3, "门禁应逼模型在收尾后再跑至少一轮"


def test_verify_gate_skips_non_code_turn(tmp_path):
    """没动过源码的轮次不触发门禁（广告/运营轮不受影响）。"""
    from ivyea_agent import agent_loop
    _git_init(tmp_path)

    class FP:
        def __init__(self): self.calls = 0
        def chat(self, messages, tools=None):
            self.calls += 1
            return {"content": "答完", "tool_calls": []}

    ctx = ToolContext(workspace=str(tmp_path))
    fp = FP()
    agent_loop.run_turn(fp, ctx, [{"role": "user", "content": "分析一下"}], narrate=lambda s: None)
    assert fp.calls == 1                                                 # 一轮就收尾，无门禁


# ── Phase 3：通用自我批判 ─────────────────────────────────────────────
def test_critique_degrades_without_provider():
    from ivyea_agent import critique
    assert critique.critique("t", "a", None)["ok"] is False


def test_critique_with_provider():
    from ivyea_agent import critique

    class FP:
        def complete(self, system, user, **k): return "**建议修正**：漏了边界。"
    r = critique.critique("任务", "回答", FP())
    assert r["ok"] and "建议修正" in r["markdown"]


def test_self_critique_tool():
    from ivyea_agent.agent_tools import dispatch

    class FP:
        def complete(self, system, user, **k): return "未见明显问题。"
    out = dispatch("self_critique", {"draft": "我的最终答案"}, ToolContext(provider=FP()))
    assert "未见明显问题" in out
    assert "draft 为空" in dispatch("self_critique", {"draft": " "}, ToolContext(provider=FP()))


# ── Phase 4：多轮修复默认 ─────────────────────────────────────────────
def test_run_loop_default_two_rounds():
    import inspect
    from ivyea_agent import code_agent
    assert inspect.signature(code_agent.run_loop).parameters["max_rounds"].default == 2
