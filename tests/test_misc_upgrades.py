"""第 6 批小项：run_command 输出落盘 / --permission-mode policy / estimate_tokens 校准。"""
from __future__ import annotations

import os

import pytest


# ── 6a run_command 超长输出全量落盘 ──

@pytest.mark.skipif(os.name == "nt", reason="命令用 python3，Windows 上无该别名")
def test_run_command_long_output_spilled_to_disk(ivyea_home):
    from ivyea_agent import tools_general as tg
    from ivyea_agent.agent_tools import ToolContext
    from ivyea_agent.permission import PermissionState
    ctx = ToolContext(perm=PermissionState(accept_edits=True), session_id="sid-spill")
    out = tg.t_run_command({"command": "python3 -c \"print('x' * 9000)\""}, ctx)
    assert "已截断" in out and "完整输出已保存" in out
    path = out.split("完整输出已保存：")[1].split("，")[0]
    with open(path, encoding="utf-8") as fh:
        full = fh.read()
    assert full.count("x") == 9000            # 全量在盘上
    assert "sid-spill" in path                # 按会话分目录


def test_run_command_short_output_no_spill(ivyea_home):
    from ivyea_agent import tools_general as tg
    from ivyea_agent.agent_tools import ToolContext
    from ivyea_agent.permission import PermissionState
    ctx = ToolContext(perm=PermissionState(accept_edits=True))
    out = tg.t_run_command({"command": "echo hi"}, ctx)
    assert "hi" in out and "完整输出已保存" not in out


# ── 6b --permission-mode policy（policy_auto 无人值守判定）──

def _no_tui(monkeypatch):
    """policy 档绝不弹交互：tui.select 被调用即失败。"""
    from ivyea_agent import tui
    monkeypatch.setattr(tui, "select", lambda *a, **k: pytest.fail("policy 档不应弹交互审批"))


def test_policy_auto_run_command_allow_and_deny(ivyea_home, monkeypatch):
    from ivyea_agent import permission
    _no_tui(monkeypatch)
    st = permission.PermissionState(policy_auto=True)
    assert permission.request_intent({"op_type": "run_command", "command": "ls -la"},
                                     "运行命令", st) == permission.APPROVE
    assert permission.request_intent({"op_type": "run_command", "command": "rm -rf /"},
                                     "运行命令", st) == permission.DENY
    assert st.aborted is False                 # 拒绝不终止整轮


def test_policy_auto_write_path_scope(ivyea_home, monkeypatch, tmp_path):
    from ivyea_agent import config, permission
    import json as _json
    _no_tui(monkeypatch)
    (config.IVYEA_DIR).mkdir(parents=True, exist_ok=True)
    (config.IVYEA_DIR / "policy.json").write_text(_json.dumps(
        {"file_write_roots": [str(tmp_path / "ok")]}), encoding="utf-8")
    st = permission.PermissionState(policy_auto=True)
    assert permission.request_intent({"op_type": "write_file", "path": str(tmp_path / "ok" / "a.txt")},
                                     "写文件", st) == permission.APPROVE
    assert permission.request_intent({"op_type": "write_file", "path": str(tmp_path / "no" / "b.txt")},
                                     "写文件", st) == permission.DENY


def test_policy_auto_denies_domain_and_unknown_writes(ivyea_home, monkeypatch):
    from ivyea_agent import permission
    from ivyea_agent.actions import Action
    _no_tui(monkeypatch)
    st = permission.PermissionState(policy_auto=True)
    assert permission.request_intent({"op_type": "lingxing_write"}, "领星写", st) == permission.DENY
    assert permission.request_intent({"op_type": "run_python", "command": "x"}, "跑py", st) == permission.DENY
    a = Action(kind="negative", search_term="bad term")
    assert permission.request(a, st) == permission.DENY   # 广告域 Action 一律 DENY


def test_permission_mode_cli_flags(ivyea_home, monkeypatch, capsys):
    """--permission-mode policy 设 policy_auto；--approve-all 别名仍生效。"""
    from ivyea_agent import providers
    from ivyea_agent.cli import build_parser
    seen = {}

    class _P:
        def stream_chat(self, messages, tools=None, **kw):
            yield {"type": "final", "content": "ok", "tool_calls": [], "usage": {}}

    def _capture(mcfg, key, narrate=None):
        return _P()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(providers, "build_chain", _capture)
    import ivyea_agent.agent_loop as al
    orig = al.run_turn_stream

    def _spy(provider, ctx, messages, **kw):
        seen["policy_auto"] = ctx.perm.policy_auto
        seen["accept_edits"] = ctx.perm.accept_edits
        return orig(provider, ctx, messages, **kw)

    monkeypatch.setattr(al, "run_turn_stream", _spy)
    parser = build_parser()
    assert parser.parse_args(["chat", "-p", "hi", "--permission-mode", "policy"]).func(
        parser.parse_args(["chat", "-p", "hi", "--permission-mode", "policy"])) == 0
    assert seen["policy_auto"] is True and seen["accept_edits"] is False
    assert parser.parse_args(["chat", "-p", "hi", "--approve-all"]).func(
        parser.parse_args(["chat", "-p", "hi", "--approve-all"])) == 0
    assert seen["accept_edits"] is True


# ── 6c estimate_tokens CJK 校准 ──

def test_estimate_tokens_cjk_weighted(ivyea_home):
    from ivyea_agent import context
    cn = [{"role": "user", "content": "中" * 1000}]
    en = [{"role": "user", "content": "a" * 1000}]
    est_cn = context.estimate_tokens(cn)
    est_en = context.estimate_tokens(en)
    assert 700 <= est_cn <= 800                 # 1000 汉字 ≈ 750 tok（旧算法给 333，低估一半+）
    assert 240 <= est_en <= 290                 # 1000 英文字符 ≈ 263 tok


def test_estimate_tokens_multimodal_skips_image_blocks(ivyea_home):
    from ivyea_agent import context
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "看图" * 10},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}}]}]
    assert context.estimate_tokens(msgs) < 100  # base64 不计入（否则高估几十倍）
