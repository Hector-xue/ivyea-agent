"""skill_view / skill_write：agent 终于能读全文、也能自己沉淀技能。"""
from __future__ import annotations

import importlib

from ivyea_agent import skill_usage, skills, tools_general
from ivyea_agent.agent_tools import ToolContext


def _ctx(**kw):
    ctx = ToolContext(workspace=".", **kw)
    ctx.perm.accept_edits = True      # 审批放行，用例验的是写入逻辑不是审批弹窗
    return ctx


def _good_args(**kw):
    args = {"action": "write", "skill_id": "lingxing.ad_patrol", "name": "lingxing-ad-patrol",
            "description": "跑一遍领星广告巡检并给出动作建议。",
            "triggers": ["广告巡检", "领星", "否词"],
            "body": "# 领星广告巡检\n\n## 何时使用\n- 每周复盘\n\n## 步骤\n1. 拉数据\n\n## 验证\n- 有输出"}
    args.update(kw)
    return args


# ── skill_write ──────────────────────────────────────────────────────────────
def test_write_creates_a_loadable_skill(ivyea_home):
    importlib.reload(skills)
    out = tools_general.t_skill_write(_good_args(), _ctx())
    assert "已新建技能" in out
    importlib.reload(skills)
    sk = skills.get_skill("lingxing.ad_patrol")
    assert sk is not None
    assert sk.description == "跑一遍领星广告巡检并给出动作建议。"
    assert sk.triggers == ["广告巡检", "领星", "否词"]
    assert "每周复盘" in sk.body


def test_a_skill_that_would_be_unfindable_is_rejected(ivyea_home):
    """没有触发词 = 中文用户搜不到。这种半成品不该落盘。"""
    importlib.reload(skills)
    out = tools_general.t_skill_write(_good_args(triggers=[]), _ctx())
    assert "校验没过" in out and "triggers" in out
    importlib.reload(skills)
    assert skills.get_skill("lingxing.ad_patrol") is None      # 一个字都没写


def test_validation_warnings_do_not_block(ivyea_home):
    importlib.reload(skills)
    out = tools_general.t_skill_write(_good_args(body="# 就一句话"), _ctx())
    assert "已新建技能" in out
    assert "建议" in out or "小节" in out


def test_rewriting_an_existing_skill_says_so(ivyea_home):
    importlib.reload(skills)
    tools_general.t_skill_write(_good_args(), _ctx())
    importlib.reload(skills)
    out = tools_general.t_skill_write(_good_args(description="改了描述。"), _ctx())
    assert "已改写技能" in out


def test_write_file_lands_next_to_the_skill(ivyea_home):
    importlib.reload(skills)
    tools_general.t_skill_write(_good_args(), _ctx())
    out = tools_general.t_skill_write(
        {"action": "write_file", "skill_id": "lingxing.ad_patrol",
         "file_path": "references/ch01.md", "content": "第一章内容"}, _ctx())
    assert "已写入" in out
    importlib.reload(skills)
    sk = skills.get_skill("lingxing.ad_patrol")
    assert skills.list_assets(sk) == ["references/ch01.md"]
    assert skills.read_asset(sk, "references/ch01.md") == "第一章内容"


def test_asset_paths_cannot_escape_the_skill_directory(ivyea_home):
    """路径来自模型，`../../` 一路能写到任何地方。"""
    importlib.reload(skills)
    tools_general.t_skill_write(_good_args(), _ctx())
    out = tools_general.t_skill_write(
        {"action": "write_file", "skill_id": "lingxing.ad_patrol",
         "file_path": "../../../../etc/pwned", "content": "x"}, _ctx())
    assert "写入失败" in out
    assert not (ivyea_home / "etc").exists()


def test_archive_moves_instead_of_deleting(ivyea_home):
    importlib.reload(skills)
    tools_general.t_skill_write(_good_args(), _ctx())
    importlib.reload(skills)
    out = tools_general.t_skill_write(
        {"action": "archive", "skill_id": "lingxing.ad_patrol"}, _ctx())
    assert "已归档" in out
    importlib.reload(skills)
    assert skills.get_skill("lingxing.ad_patrol") is None
    assert len(skills.list_archive()) == 1
    skills.restore_skill(skills.list_archive()[0])
    importlib.reload(skills)
    assert skills.get_skill("lingxing.ad_patrol") is not None      # 拿得回来


def test_builtin_skills_cannot_be_archived(ivyea_home):
    importlib.reload(skills)
    out = tools_general.t_skill_write(
        {"action": "archive", "skill_id": "amazon.budget_pacing"}, _ctx())
    assert "归档失败" in out
    importlib.reload(skills)
    assert skills.get_skill("amazon.budget_pacing") is not None


def test_plan_mode_blocks_skill_writes(ivyea_home):
    importlib.reload(skills)
    ctx = ToolContext(workspace=".", plan_mode=True)
    out = tools_general.t_skill_write(_good_args(), ctx)
    assert "计划模式" in out
    importlib.reload(skills)
    assert skills.get_skill("lingxing.ad_patrol") is None


# ── skill_view ───────────────────────────────────────────────────────────────
def test_view_returns_the_full_body(ivyea_home):
    """自动注入只给开头一段，全文此前**根本够不着** —— 这个工具补的就是这个洞。"""
    importlib.reload(skills)
    long_body = "# 手册\n\n## 何时使用\n用它\n\n" + "\n".join(f"第 {i} 步：做点什么。" for i in range(200))
    tools_general.t_skill_write(_good_args(body=long_body), _ctx())
    importlib.reload(skills)

    text, _ids = skills.context_for_query("广告巡检", limit=2)
    assert "第 199 步" not in text          # 注入的是开头一段
    assert "skill_view" in text             # 但告诉了模型怎么拿全文

    full = tools_general.t_skill_view({"skill_id": "lingxing.ad_patrol"}, _ctx())
    assert "第 199 步" in full              # 拿得到


def test_view_reads_an_asset(ivyea_home):
    importlib.reload(skills)
    tools_general.t_skill_write(_good_args(), _ctx())
    tools_general.t_skill_write(
        {"action": "write_file", "skill_id": "lingxing.ad_patrol",
         "file_path": "references/ch01.md", "content": "第一章内容"}, _ctx())
    importlib.reload(skills)
    out = tools_general.t_skill_view(
        {"skill_id": "lingxing.ad_patrol", "file_path": "references/ch01.md"}, _ctx())
    assert out == "第一章内容"


def test_view_lists_assets_when_the_path_is_wrong(ivyea_home):
    importlib.reload(skills)
    tools_general.t_skill_write(_good_args(), _ctx())
    tools_general.t_skill_write(
        {"action": "write_file", "skill_id": "lingxing.ad_patrol",
         "file_path": "references/ch01.md", "content": "x"}, _ctx())
    importlib.reload(skills)
    out = tools_general.t_skill_view(
        {"skill_id": "lingxing.ad_patrol", "file_path": "references/没有这个.md"}, _ctx())
    assert "references/ch01.md" in out      # 别只说"没有"，把有什么告诉它


def test_view_of_an_unknown_skill_suggests_neighbours(ivyea_home):
    importlib.reload(skills)
    out = tools_general.t_skill_view({"skill_id": "预算"}, _ctx())
    assert "未找到" in out and "budget_pacing" in out


def test_view_cannot_escape_the_skill_directory(ivyea_home):
    importlib.reload(skills)
    tools_general.t_skill_write(_good_args(), _ctx())
    importlib.reload(skills)
    out = tools_general.t_skill_view(
        {"skill_id": "lingxing.ad_patrol", "file_path": "../../../../etc/passwd"}, _ctx())
    assert "超出技能目录" in out


# ── 使用统计 ─────────────────────────────────────────────────────────────────
def test_injection_and_view_are_counted(ivyea_home):
    importlib.reload(skills)
    skills.context_for_query("预算怎么分配", limit=2)
    tools_general.t_skill_view({"skill_id": "amazon.budget_pacing"}, _ctx())
    row = skill_usage.stats("amazon.budget_pacing")
    assert row["hits"] >= 2
    assert row["by_source"]["inject"] >= 1
    assert row["by_source"]["view"] >= 1


def test_dormant_lists_never_hit_skills(ivyea_home):
    importlib.reload(skills)
    skills.context_for_query("预算怎么分配", limit=2)
    ids = [sk.id for sk in skills.list_skills()]
    dormant = skill_usage.dormant(ids)
    assert "amazon.budget_pacing" not in dormant
    assert "amazon.listing_conversion_audit" in dormant


def test_a_corrupt_usage_file_is_survivable(ivyea_home):
    skill_usage.usage_file().parent.mkdir(parents=True, exist_ok=True)
    skill_usage.usage_file().write_text("{半截", encoding="utf-8")
    skill_usage.record("a.b", query="x")
    assert skill_usage.stats("a.b")["hits"] == 1
