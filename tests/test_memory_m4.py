"""M4 记忆补全：持久指令、会话回忆、摘要入库、自策展、/init。"""
from __future__ import annotations


def _mem():
    from ivyea_agent import memory
    return memory


def test_load_instructions(ivyea_home):
    memory = _mem()
    (ivyea_home / "USER.md").write_text("我是 3C 类目卖家，偏保守。", encoding="utf-8")
    (ivyea_home / "AGENTS.md").write_text("否词≥15点击0单；保护品牌词。", encoding="utf-8")
    s = memory.load_instructions()
    assert "3C 类目" in s and "保护品牌词" in s
    assert "# USER.md" in s and "# AGENTS.md" in s


def test_load_instructions_empty(ivyea_home):
    assert _mem().load_instructions() == ""


def test_project_agents_md(ivyea_home, tmp_path):
    memory = _mem()
    (tmp_path / "AGENTS.md").write_text("项目级:只看 US 站。", encoding="utf-8")
    s = memory.load_instructions(str(tmp_path))
    assert "只看 US 站" in s


def test_index_turn_recall(ivyea_home):
    memory = _mem()
    memory.index_turn("user", "帮我看看 karaoke remote 这个搜索词要不要否")
    hits = memory.search("karaoke")
    assert any("karaoke" in h["text"] for h in hits)


def test_remember_summary_recall(ivyea_home):
    memory = _mem()
    memory.remember_summary("本会话:对 B0X 否了3个词，毛利率35%，目标ACOS 24.5%。")
    hits = memory.search("目标ACOS")
    assert any("目标ACOS" in h["text"] for h in hits)


def test_nudge_hint():
    memory = _mem()
    assert memory.nudge_hint("建议把这个词否词处理") != ""
    assert "记住" in memory.nudge_hint("降bid 到 0.85")
    assert memory.nudge_hint("今天天气不错") == ""
    assert memory.nudge_hint("我已经帮你记住了这条否词") == ""  # 已含"记住"不再提示


def test_init_agents_once(ivyea_home):
    memory = _mem()
    path = str(ivyea_home / "AGENTS.md")
    created, p = memory.init_agents(path)
    assert created and "账户运营指令" in open(p, encoding="utf-8").read()
    created2, _ = memory.init_agents(path)
    assert not created2  # 不覆盖
