"""技能写入校验闸：规范是代码，不是 prompt 里的祈祷。"""
from __future__ import annotations

from ivyea_agent import skill_authoring


def _meta(**kw):
    base = {"name": "lingxing-ad-patrol", "description": "跑一遍领星广告巡检并给出动作建议。",
            "triggers": ["广告巡检", "领星", "否词"]}
    base.update(kw)
    return base


def _body(extra=""):
    return ("# 领星广告巡检\n\n两句话说清做什么。\n\n"
            "## 何时使用\n- 每周复盘广告\n\n## 前置条件\n- 配好领星 key\n\n"
            "## 怎么跑\n用 `run_patrol` 起一轮。\n\n## 速查\n- run_patrol\n\n"
            "## 步骤\n1. 拉数据\n\n## 坑\n- 数据不够别动手\n\n## 验证\n- 看有没有输出\n" + extra)


def test_a_good_skill_passes_clean():
    res = skill_authoring.validate(_meta(), _body(), known_knowledge=lambda _k: True)
    assert res["ok"] is True
    assert res["warnings"] == []


# ── 硬规则：不满足就找不到 / 用不了 ──────────────────────────────────────────
def test_missing_triggers_is_an_error_not_a_warning():
    """检索不分词，中文查询几乎完全靠触发词命中 —— 没有触发词的技能中文用户搜不到。"""
    res = skill_authoring.validate(_meta(triggers=[]), _body(), known_knowledge=lambda _k: True)
    assert res["ok"] is False
    assert any("triggers" in e for e in res["errors"])


def test_bad_name_is_rejected():
    for bad in ("Lingxing Ad Patrol", "lingxing_ad_patrol", "LINGXING", "ad--patrol-"):
        res = skill_authoring.validate(_meta(name=bad), _body(), known_knowledge=lambda _k: True)
        assert res["ok"] is False, bad


def test_missing_description_is_rejected():
    res = skill_authoring.validate(_meta(description=""), _body(), known_knowledge=lambda _k: True)
    assert res["ok"] is False


def test_empty_body_is_rejected():
    res = skill_authoring.validate(_meta(), "", known_knowledge=lambda _k: True)
    assert res["ok"] is False


def test_nonexistent_knowledge_card_is_rejected():
    res = skill_authoring.validate(_meta(knowledge_ids=["amazon.不存在"]), _body(),
                                   known_knowledge=lambda _k: None)
    assert res["ok"] is False
    assert any("不存在" in e for e in res["errors"])


def test_an_unreadable_knowledge_store_never_blocks():
    def boom(_k):
        raise RuntimeError("知识库挂了")
    res = skill_authoring.validate(_meta(knowledge_ids=["x"]), _body(), known_knowledge=boom)
    assert res["ok"] is True


# ── 软规则：写入照常，但要说 ─────────────────────────────────────────────────
def test_long_description_warns_but_passes():
    res = skill_authoring.validate(_meta(description="说明。" * 40), _body(),
                                   known_knowledge=lambda _k: True)
    assert res["ok"] is True
    assert any("建议压到" in w for w in res["warnings"])


def test_absurd_description_is_rejected():
    res = skill_authoring.validate(_meta(description="说明。" * 200), _body(),
                                   known_knowledge=lambda _k: True)
    assert res["ok"] is False


def test_marketing_words_and_name_echo_warn():
    res = skill_authoring.validate(
        _meta(description="一个强大的 lingxing-ad-patrol。"), _body(), known_knowledge=lambda _k: True)
    assert res["ok"] is True
    assert any("营销词" in w for w in res["warnings"])
    assert any("复读" in w for w in res["warnings"])


def test_long_triggers_warn():
    res = skill_authoring.validate(_meta(triggers=["帮我把领星的广告巡检跑一遍然后给建议"]), _body(),
                                   known_knowledge=lambda _k: True)
    assert res["ok"] is True
    assert any("太长" in w for w in res["warnings"])


def test_missing_sections_warn():
    res = skill_authoring.validate(_meta(), "# 标题\n\n就一句话。", known_knowledge=lambda _k: True)
    assert res["ok"] is True
    assert any("小节" in w for w in res["warnings"])


def test_invented_tool_names_are_flagged():
    """"不许发明命令"的可检查版本：正文里 `skill_xxx` 这种词，我们查得出真假。"""
    res = skill_authoring.validate(_meta(), _body("\n用 `skill_autogenerate` 一键生成。\n"),
                                   known_knowledge=lambda _k: True)
    assert res["ok"] is True
    assert any("不存在" in w and "skill_autogenerate" in w for w in res["warnings"])


def test_real_tool_names_are_not_flagged():
    res = skill_authoring.validate(_meta(), _body("\n用 `skill_view` 读全文、`run_patrol` 巡检。\n"),
                                   known_knowledge=lambda _k: True)
    assert not any("不存在" in w for w in res["warnings"])


def test_ordinary_backticked_words_are_not_flagged():
    """只对**看起来像我们工具**的词报警，不然满篇变量名全成误报。"""
    res = skill_authoring.validate(_meta(), _body("\n字段 `campaign_id`、`match_type`。\n"),
                                   known_knowledge=lambda _k: True)
    assert not any("不存在" in w for w in res["warnings"])


# ── frontmatter 渲染 ─────────────────────────────────────────────────────────
def test_frontmatter_round_trips_through_the_loader():
    from ivyea_agent import skills
    text = skill_authoring.render_frontmatter(_meta(), _body())
    fm, body = skills._parse_frontmatter(text)
    assert fm["name"] == "lingxing-ad-patrol"
    assert fm["triggers"] == ["广告巡检", "领星", "否词"]
    assert body.startswith("# 领星广告巡检")


def test_colons_in_a_description_do_not_break_the_yaml():
    """中文描述里冒号很常见；不加引号 YAML 会把它解析成映射然后**静默**丢掉整段。"""
    from ivyea_agent import skills
    meta = _meta(description="做一件事：把广告巡检跑完")
    fm, _body_text = skills._parse_frontmatter(skill_authoring.render_frontmatter(meta, _body()))
    assert fm["description"] == "做一件事：把广告巡检跑完"


def test_trigger_length_is_measured_per_script():
    """中文一个字就是一个词，英文一个词好几个字符 —— 同一个数字会误伤英文触发词。"""
    assert skill_authoring._trigger_too_long("广告巡检") is False
    assert skill_authoring._trigger_too_long("帮我把广告巡检跑一遍") is True
    assert skill_authoring._trigger_too_long("budget-pacing") is False
    assert skill_authoring._trigger_too_long("search-term-report-optimizer-workflow") is True
