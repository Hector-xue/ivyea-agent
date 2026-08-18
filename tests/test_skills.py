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


# ── SKILL.md + frontmatter（业界通行格式）─────────────────────────────────────


def _write_skill(root, rel: str, frontmatter: str, body: str = "步骤一。", assets=None):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    for path, content in (assets or {}).items():
        f = d / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    return d


def test_frontmatter_skill_loads_without_a_skill_json(ivyea_home, monkeypatch):
    """外部技能库（IvyeaOps 的 Skill 中心就是）用的就是这个格式，不该再要求 skill.json。"""
    import importlib
    importlib.reload(skills)
    _write_skill(ivyea_home / "skills", "amazon/search-term",
                 "name: search-term\ndescription: Analyze search term reports\n"
                 "description_zh: 分析广告搜索词报表\nversion: 2.0.0\n"
                 "triggers: [搜索词, 否词]")

    rows = {s.id: s for s in skills.list_skills()}
    sk = rows.get("amazon.search_term")
    assert sk is not None
    assert sk.version == "2.0.0"
    assert sk.description == "分析广告搜索词报表"      # 中文描述优先
    assert sk.triggers == ["搜索词", "否词"]
    assert sk.domain == "amazon"


def test_hermes_style_tags_become_triggers(ivyea_home):
    import importlib
    importlib.reload(skills)
    _write_skill(ivyea_home / "skills", "amazon/legacy",
                 "name: legacy\ndescription: d\n"
                 "metadata:\n  hermes:\n    tags: [ads, 报表]")
    sk = {s.id: s for s in skills.list_skills()}["amazon.legacy"]
    assert sk.triggers == ["ads", "报表"]


def test_skill_json_still_wins_for_existing_skills(ivyea_home):
    """老技能一个都不能受影响。"""
    import importlib
    importlib.reload(skills)
    d = _write_skill(ivyea_home / "skills", "amazon/both",
                     "name: both\ndescription: 来自 frontmatter")
    (d / "skill.json").write_text(json.dumps(
        {"id": "amazon.both", "description": "来自 skill.json"}), encoding="utf-8")
    sk = {s.id: s for s in skills.list_skills()}["amazon.both"]
    assert sk.description == "来自 skill.json"


def test_external_roots_are_loaded_in_place(ivyea_home, tmp_path, monkeypatch):
    """上游把自己的技能库**原地**挂上来，不用复制、不用转格式。"""
    import importlib
    external = tmp_path / "hub" / "amazon"
    _write_skill(external, "market-research", "name: market-research\ndescription_zh: 市场调研")
    monkeypatch.setenv("IVYEA_SKILL_ROOTS", str(external))
    importlib.reload(skills)

    sk = {s.id: s for s in skills.list_skills()}.get("amazon.market_research")
    assert sk is not None
    assert sk.scope == "external"
    # 目录名就是 domain —— 把 .../skills/amazon 整个挂上来也能得到正确的域
    assert sk.domain == "amazon"
    assert sk.path == str(external / "market-research")


def test_external_roots_can_never_shadow_builtin(ivyea_home, tmp_path, monkeypatch):
    """外部库里随手一个同名技能就顶掉内置技能，是最难查的那种故障。"""
    import importlib
    external = tmp_path / "hub" / "amazon"
    _write_skill(external, "budget-pacing",
                 "id: amazon.budget_pacing\nname: budget-pacing\ndescription_zh: 冒牌货")
    monkeypatch.setenv("IVYEA_SKILL_ROOTS", str(external))
    importlib.reload(skills)

    sk = {s.id: s for s in skills.list_skills()}["amazon.budget_pacing"]
    assert sk.scope == "builtin"
    assert "冒牌货" not in sk.description


def test_personal_skills_still_override_builtin(ivyea_home):
    """个人技能覆盖内置 —— 这是本机作者的明确意图，语义不能跟着一起改掉。"""
    import importlib
    importlib.reload(skills)
    _write_skill(ivyea_home / "skills", "amazon/mine",
                 "id: amazon.budget_pacing\nname: mine\ndescription_zh: 我自己的版本")
    sk = {s.id: s for s in skills.list_skills()}["amazon.budget_pacing"]
    assert sk.description == "我自己的版本"


def test_the_model_is_told_where_the_assets_are(ivyea_home):
    """说明书写着"运行 scripts/x.py"，就得告诉它这些文件在哪。"""
    import importlib
    importlib.reload(skills)
    d = _write_skill(ivyea_home / "skills", "amazon/with-assets",
                     "name: with-assets\ndescription_zh: 带脚本的技能",
                     body="按 scripts/render.py 渲染。",
                     assets={"scripts/render.py": "print(1)"})
    sk = {s.id: s for s in skills.list_skills()}["amazon.with_assets"]
    assert str(d) in skills.render_skill(sk)

    text, _ = skills.context_for_query("带脚本的技能", limit=2, max_chars=2000)
    assert str(d) in text


def test_no_assets_means_no_directory_noise(ivyea_home):
    import importlib
    importlib.reload(skills)
    _write_skill(ivyea_home / "skills", "amazon/plain", "name: plain\ndescription_zh: 纯说明书")
    sk = {s.id: s for s in skills.list_skills()}["amazon.plain"]
    assert "文件目录" not in skills.render_skill(sk)


def test_broken_frontmatter_does_not_take_down_the_loader(ivyea_home):
    import importlib
    importlib.reload(skills)
    _write_skill(ivyea_home / "skills", "amazon/good", "name: good\ndescription_zh: 好的")
    bad = ivyea_home / "skills" / "amazon" / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("---\n: : 坏 yaml : :\n---\n正文", encoding="utf-8")

    ids = {s.id for s in skills.list_skills()}
    assert "amazon.good" in ids
    assert "amazon.search_term_optimizer" in ids      # 内置的照常在


def test_archive_dirs_are_skipped(ivyea_home):
    import importlib
    importlib.reload(skills)
    _write_skill(ivyea_home / "skills", "amazon/.archive/old", "name: old\ndescription_zh: 归档")
    assert "amazon.old" not in {s.id for s in skills.list_skills()}


# ── 中文匹配 ────────────────────────────────────────────────────────────────


def test_chinese_queries_are_split_into_bigrams():
    """没有分词器时，整句中文会被当成一个词 —— 那样中文提问永远匹配不到技能。"""
    terms = skills._terms("做个市场调研")
    assert "市场" in terms and "调研" in terms


def test_mixed_language_tokens_still_yield_chinese_bigrams():
    terms = skills._terms("给 ASIN 做审计分析")
    assert "asin" in terms
    assert "审计" in terms


def test_a_natural_chinese_question_matches_a_builtin_skill():
    """这是这次改动要保住的东西：用户就是这么说话的。"""
    hits = skills.search("帮我看看预算怎么放量", limit=3)
    assert hits and any(sk.id == "amazon.budget_pacing" for sk, _ in hits)


def test_a_long_body_cannot_outrank_a_skill_that_is_actually_about_it(ivyea_home):
    """切了 2-gram 之后，长正文会靠噪音堆分。实测出现过一个几千字的技能在
    完全不相干的查询上排第一 —— 所以正文命中要封顶、标识和描述要加权。"""
    import importlib
    importlib.reload(skills)
    root = ivyea_home / "skills"
    _write_skill(root, "amazon/on-topic",
                 "name: on-topic\ndescription_zh: 库存周转与补货节奏",
                 body="简短。")
    _write_skill(root, "amazon/rambling",
                 "name: rambling\ndescription_zh: 别的事",
                 body=("库存 周转 补货 " * 200))

    hits = skills.search("库存周转怎么算", limit=2)
    assert hits[0][0].id == "amazon.on_topic"


def test_trigger_lists_written_with_chinese_separators_are_split(ivyea_home):
    """作者常把一行写成「调研报告、选品调研、市场分析」。不切开就是一条没法命中的长串。"""
    import importlib
    importlib.reload(skills)
    _write_skill(ivyea_home / "skills", "amazon/research",
                 "name: research\ndescription_zh: 调研\n"
                 "triggers: ['调研报告、选品调研、市场分析']")
    sk = {s.id: s for s in skills.list_skills()}["amazon.research"]
    assert "市场分析" in sk.triggers
    assert "选品调研" in sk.triggers


def test_auto_injection_requires_a_named_hit(ivyea_home):
    """自动注入只认"这条技能就是干这个的"，不认正文里撞了几个常用词。

    线上事故：一句「测试」以 score=2 命中 ASIN 审计手册（全靠正文里出现过两次
    "测试"），于是 1600 字流程被塞进上下文，把模型带进一整套审计动作。
    """
    import importlib
    importlib.reload(skills)
    root = ivyea_home / "skills"
    _write_skill(root, "amazon/audit",
                 "name: audit\ndescription_zh: ASIN 深度审计\ntriggers: ['asin审计']",
                 body="第一步先做连通性测试，再测试报表口径。测试通过后进入下一阶段。")

    # 正文里有"测试"，但这条技能不是干"测试"的 —— 自动注入必须放过
    assert skills.context_for_query("测试", limit=2) == ("", [])
    # 人工翻库不设这道闸：搜得到才好挑
    assert "amazon.audit" in [sk.id for sk, _ in skills.search("测试", limit=5)]
    # 名义命中照常注入
    # 注意：ivyea_home 夹具不隔离 settings.skill_roots，真实技能库照样在列，
    # 所以这里断言"命中里有它"，不断言"只有它"。
    text, ids = skills.context_for_query("asin审计", limit=3)
    assert "amazon.audit" in ids and text
