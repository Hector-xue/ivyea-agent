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


def test_create_user_skill_and_audit(ivyea_home):
    sk = skills.create_user_skill(
        "general.release_check",
        title="Release Check",
        description="Check release readiness.",
        triggers=["发版", "release"],
        tools=["gitops"],
        knowledge_ids=["missing.card"],
        body="# Release Check\n\nInspect git status.",
    )
    assert sk.scope == "user"
    assert sk.domain == "general"
    assert "release" in sk.triggers
    loaded = skills.get_skill("general.release_check")
    assert loaded and "Inspect git status" in loaded.body
    rows = skills.audit()
    row = next(r for r in rows if r["id"] == "general.release_check")
    assert row["ok"] is False
    assert "missing_knowledge:missing.card" in row["issues"]
    assert "Skill Audit" in skills.render_audit(rows)


def test_skill_status_and_lockfile_for_user_override(ivyea_home):
    sk = skills.create_user_skill(
        "amazon.search_term_optimizer",
        title="Local Search Optimizer",
        description="Override builtin search workflow.",
        triggers=["search term"],
        tools=["run_patrol"],
        knowledge_ids=["playbook.search_term_lifecycle"],
        body="# Local Override\n\nUse account-specific rules.",
        overwrite=True,
    )
    manifest = sk.path and (ivyea_home / "skills" / "amazon" / "search_term_optimizer" / "skill.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = "0.0.1"
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    rows = skills.status()
    row = next(r for r in rows if r["id"] == "amazon.search_term_optimizer")
    assert row["active_scope"] == "user"
    assert any(i.startswith("user_version_behind_builtin") for i in row["issues"])
    assert "Skill Status" in skills.render_status(rows)

    lock = skills.lockfile()
    active = next(s for s in lock["skills"] if s["id"] == "amazon.search_term_optimizer")
    assert active["scope"] == "user"

    out = skills.write_lockfile(ivyea_home / "skills.lock.json")
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == 1


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

    assert main([
        "skill", "create", "general.my_skill",
        "--title", "My Skill",
        "--trigger", "测试",
        "--body", "# My Skill\n\nDo it.",
    ]) == 0
    out = capsys.readouterr().out
    assert "已创建 skill：general.my_skill" in out

    assert main(["skill", "audit"]) == 0
    out = capsys.readouterr().out
    assert "Skill Audit" in out

    assert main(["skill", "status"]) == 0
    out = capsys.readouterr().out
    assert "Skill Status" in out

    assert main(["skill", "export-lock"]) == 0
    out = capsys.readouterr().out
    assert "skills.lock.json" in out
