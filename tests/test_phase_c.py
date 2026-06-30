"""Phase C：persona 开场、完整流式 _StreamPrinter 视觉行数计算。"""
from __future__ import annotations

from ivyea_agent import agent_loop
from ivyea_agent.cli import _StreamPrinter


def test_persona_opening_is_dual_role():
    sp = agent_loop.SYSTEM_PROMPT
    assert "编码" in sp[:120] or "工程" in sp[:120]   # 代码成为一等公民
    assert "亚马逊运营" in sp[:120]                     # 运营仍在


def test_stream_visual_lines_ascii_wrap():
    # 宽 10：'a'*25 → 3 行；空串 → 1 行；含换行分别算
    f = _StreamPrinter._visual_lines
    assert f("a" * 25, 10) == 3
    assert f("", 10) == 1
    assert f("abc\ndef", 10) == 2
    assert f("a" * 10, 10) == 1
    assert f("a" * 11, 10) == 2


def test_stream_visual_lines_cjk_double_width():
    f = _StreamPrinter._visual_lines
    # 中文每字宽 2：5 个中文 = 宽 10 → 正好 1 行；6 个 = 宽 12 → 2 行（宽 10）
    assert f("你好世界吗", 10) == 1
    assert f("你好世界吗啊", 10) == 2


def test_stream_block_resets_on_commit():
    sp = _StreamPrinter()
    sp.block = "some streamed text"
    sp.on = True
    sp.commit()
    assert sp.block == ""   # 工具行打断后重置当前段
