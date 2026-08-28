"""技能策展与后台沉淀：只出建议、只归档不删、默认关。"""
from __future__ import annotations

import importlib

from ivyea_agent import skill_curator, skill_reflect, skill_usage, skills


def _make(skill_id, triggers, body="# 正文\n\n## 何时使用\n- 用它"):
    skills.write_user_skill(skill_id,
                            {"name": skill_id.rpartition(".")[2].replace("_", "-"),
                             "description": f"{skill_id} 做的事", "triggers": triggers},
                            body)


# ── 策展 ─────────────────────────────────────────────────────────────────────
def test_never_hit_skills_show_up_as_dormant(ivyea_home):
    importlib.reload(skills)
    _make("mine.alpha", ["阿尔法", "甲", "一号"])
    _make("mine.beta", ["贝塔", "乙", "二号"])
    importlib.reload(skills)
    skill_usage.record("mine.alpha", query="阿尔法")

    report = skill_curator.analyze()
    dormant = {r["id"] for r in report["dormant"]}
    assert "mine.beta" in dormant
    assert "mine.alpha" not in dormant


def test_builtin_skills_are_never_curated(ivyea_home):
    """内置技能随包发布，归档了下次升级又回来，纯属白折腾。"""
    importlib.reload(skills)
    report = skill_curator.analyze()
    assert not any(r["id"].startswith("amazon.") for r in report["dormant"])


def test_overlapping_triggers_are_flagged(ivyea_home):
    importlib.reload(skills)
    _make("mine.patrol_a", ["广告巡检", "否词", "搜索词", "领星"])
    _make("mine.patrol_b", ["广告巡检", "否词", "搜索词", "报表"])
    importlib.reload(skills)
    report = skill_curator.analyze()
    assert report["overlapping"]
    pair = report["overlapping"][0]
    assert set(pair["ids"]) == {"mine.patrol_a", "mine.patrol_b"}
    assert "广告巡检" in pair["shared"]


def test_a_couple_of_triggers_is_not_enough_to_judge_overlap(ivyea_home):
    """只有一两个触发词时重合率没有意义，别拿它当证据。"""
    importlib.reload(skills)
    _make("mine.x", ["巡检"])
    _make("mine.y", ["巡检"])
    importlib.reload(skills)
    assert skill_curator.analyze()["overlapping"] == []


def test_a_user_override_is_not_a_defect(ivyea_home):
    """个人技能覆盖内置是明确意图，不该被当成"有问题"报出来。"""
    importlib.reload(skills)
    skills.write_user_skill("amazon.budget_pacing",
                            {"name": "budget-pacing", "description": "我自己的版本",
                             "triggers": ["预算", "分配", "节奏"]}, "# 我的版本")
    importlib.reload(skills)
    report = skill_curator.analyze()
    assert not any(r["id"] == "amazon.budget_pacing" for r in report["unhealthy"])


def test_render_is_readable_and_says_what_to_do(ivyea_home):
    importlib.reload(skills)
    _make("mine.dead", ["死的", "没人用", "沉睡"])
    importlib.reload(skills)
    out = skill_curator.render()
    assert "mine.dead" in out
    assert "archive" in out and "restore" in out      # 建议里要说清可恢复


def test_apply_only_archives_and_only_the_dormant(ivyea_home):
    importlib.reload(skills)
    _make("mine.dead", ["死的", "没人用", "沉睡"])
    _make("mine.alive", ["活的", "常用", "热门"])
    importlib.reload(skills)
    skill_usage.record("mine.alive", query="活的")

    done = skill_curator.apply_archive()
    importlib.reload(skills)
    assert done == ["mine.dead"]
    assert skills.get_skill("mine.dead") is None
    assert skills.get_skill("mine.alive") is not None
    assert skills.list_archive()                       # 只是移走了，没删


def test_overlap_and_defects_are_never_auto_applied(ivyea_home):
    """合并要判断哪条更好、补齐要写内容 —— 这两件事没有程序能替用户拍板。"""
    importlib.reload(skills)
    _make("mine.a", ["广告巡检", "否词", "搜索词"])
    _make("mine.b", ["广告巡检", "否词", "搜索词"])
    importlib.reload(skills)
    skill_usage.record(["mine.a", "mine.b"], query="广告巡检")   # 都活着，不沉睡
    assert skill_curator.apply_archive() == []
    importlib.reload(skills)
    assert skills.get_skill("mine.a") and skills.get_skill("mine.b")


def test_an_empty_library_is_not_an_error(ivyea_home):
    importlib.reload(skills)
    assert "没什么可策展" in skill_curator.render()


# ── 后台沉淀 ─────────────────────────────────────────────────────────────────
def test_auto_learn_is_off_by_default(ivyea_home):
    assert skill_reflect.enabled() is False
    assert skill_reflect.should_reflect(tool_steps=99, had_phases=True, had_evidence=True) is False
    assert skill_reflect.maybe_reflect_async("经过", tool_steps=99, had_phases=True,
                                             had_evidence=True) is False


def test_the_significance_gate(ivyea_home):
    from ivyea_agent import config
    config.set_setting("skill_auto_learn", True)
    assert skill_reflect.should_reflect(tool_steps=2, had_phases=True, had_evidence=True) is False
    assert skill_reflect.should_reflect(tool_steps=99, had_phases=False, had_evidence=True) is False
    assert skill_reflect.should_reflect(tool_steps=99, had_phases=True, had_evidence=False) is False
    assert skill_reflect.should_reflect(tool_steps=99, had_phases=True, had_evidence=True) is True


def test_the_evidence_gate_needs_repeat_sightings(ivyea_home):
    """一次性的具体任务不是技能。同一类流程跨会话反复出现才算数。"""
    kind = "跑领星广告巡检并出动作"
    need = skill_reflect.PROMOTE_AFTER_SIGHTINGS
    for i in range(1, need):
        assert skill_reflect.note_sighting(kind, summary=f"第 {i} 次") == i
        assert skill_reflect.ready_to_promote(kind) is False
    assert skill_reflect.note_sighting(kind, summary="最后一次") == need
    assert skill_reflect.ready_to_promote(kind) is True
    skill_reflect.clear_pending(kind)
    assert skill_reflect.ready_to_promote(kind) is False


def test_the_skill_gate_is_never_looser_than_the_memory_gate():
    """建一条技能比记一条记忆贵得多（会被自动注入、会抢命中）。门槛不许更松。

    初版这里写死成 2，比 memory_reflect 的 3 还低 —— 方向反了。
    """
    from ivyea_agent import memory_reflect
    assert skill_reflect.PROMOTE_AFTER_SIGHTINGS >= memory_reflect.PROMOTE_AFTER_SIGHTINGS


def test_sighting_keys_are_normalised(ivyea_home):
    skill_reflect.note_sighting("跑 领星  广告巡检")
    assert skill_reflect.note_sighting("跑 领星 广告巡检") == 2


def test_pending_is_capped(ivyea_home):
    for i in range(skill_reflect.MAX_PENDING + 20):
        skill_reflect.note_sighting(f"流程 {i}")
    assert len(skill_reflect._load_pending()) <= skill_reflect.MAX_PENDING


def test_the_reflect_agent_only_gets_skill_tools():
    from ivyea_agent import agent_tools
    names = {t["function"]["name"] for t in agent_tools.TOOL_SCHEMAS
             if t["function"]["name"] in skill_reflect.REFLECT_TOOLS}
    assert names == set(skill_reflect.REFLECT_TOOLS)
    assert "run_command" not in skill_reflect.REFLECT_TOOLS
    assert "write_file" not in skill_reflect.REFLECT_TOOLS


def test_the_prompt_refuses_to_manufacture_a_skill():
    p = skill_reflect.build_prompt("经过")
    assert "不值得沉淀" in p
    assert "不要**为了有产出硬写" in p or "不要" in p
    assert "下次遇到同类问题，会不会还这么干" in p


def test_a_corrupt_pending_file_is_survivable(ivyea_home):
    skill_reflect._pending_file().parent.mkdir(parents=True, exist_ok=True)
    skill_reflect._pending_file().write_text("{半截", encoding="utf-8")
    assert skill_reflect.note_sighting("某流程") == 1
