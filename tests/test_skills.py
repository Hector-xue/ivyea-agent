from __future__ import annotations

import json

from ivyea_agent import skills


def test_builtin_skills_list_and_get():
    rows = skills.list_skills(include_user=False)
    ids = {s.id for s in rows}
    assert "amazon.search_term_optimizer" in ids
    assert "amazon.negative_keyword_guard" in ids
    assert "amazon.listing_conversion_audit" in ids

    sk = skills.get_skill("amazon.search_term_optimizer")
    assert sk
    assert "run_patrol" in sk.tools
    assert "playbook.search_term_lifecycle" in sk.knowledge_ids
    assert "Search Term Optimizer" in skills.render_skill(sk)


def test_skill_search_chinese_terms():
    hits = skills.search("否词 误伤 negative", limit=3)
    assert hits
    assert any(sk.id == "amazon.negative_keyword_guard" for sk, _ in hits)

    text, ids = skills.context_for_query("新品 自动广告 测词", limit=2, max_chars=900)
    assert "skill:amazon.launch_playbook" in text
    assert "amazon.launch_playbook" in ids


def test_user_skill_directory(ivyea_home):
    base = ivyea_home / "skills" / "amazon" / "custom_review"
    base.mkdir(parents=True)
    (base / "skill.json").write_text(json.dumps({
        "id": "amazon.custom_review",
        "title": "Custom Review",
        "domain": "amazon",
        "version": "local",
        "description": "User-defined weekly review.",
        "triggers": ["自定义复盘"],
        "knowledge_ids": ["playbook.report_driven_optimization"],
        "tools": ["knowledge_search"],
    }), encoding="utf-8")
    (base / "SKILL.md").write_text("# Custom Review\n\nFollow local account rules.", encoding="utf-8")

    sk = skills.get_skill("amazon.custom_review")
    assert sk and sk.scope == "user"
    assert "Custom Review" in skills.render_list([sk])


def test_skill_cli(capsys):
    from ivyea_agent.cli import main

    assert main(["skill", "list"]) == 0
    out = capsys.readouterr().out
    assert "amazon.search_term_optimizer" in out

    assert main(["skill", "search", "listing", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "amazon.listing_conversion_audit" in out

    assert main(["skill", "show", "amazon.budget_pacing"]) == 0
    out = capsys.readouterr().out
    assert "Amazon Budget Pacing" in out
