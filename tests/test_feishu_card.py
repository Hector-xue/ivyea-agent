"""飞书卡片构建测试 —— 纯函数，无需凭据即可锁死结构与安全性。"""
from __future__ import annotations

import json

import pytest


def _finding(**over):
    from ivyea_agent import store_health

    kw = dict(code="ads.spend_burst", layer="L2", severity="crit",
              action_class=store_health.STANCH, sid=1, scope="campaign",
              target_id="C1", target_name="FK50-Auto",
              message="活动「FK50-Auto」花费突增：近 1.0 小时花 890.00",
              window="近 1.0 小时", provenance="日内采样 · 同时段历史均值",
              evidence={"spend_delta": 890.0, "baseline_per_hour": 310.0},
              intent={"op_type": "campaign_budget", "sid": 1, "target_id": "C1",
                      "change": {"daily_budget": 85.0},
                      "before": {"daily_budget": 100.0}})
    kw.update(over)
    return store_health.Finding(**kw)


def _dump(card):
    return json.dumps(card, ensure_ascii=False)


# ── schema（ADR-4：卡片 1.0）────────────────────────────────────────────────
def test_card_uses_v1_schema(ivyea_home):
    from ivyea_agent import feishu_card as fc

    card = fc.build_finding_card(_finding(), "ap1")
    assert card["config"] == {"wide_screen_mode": True}
    assert set(card) == {"config", "header", "elements"}
    assert card["header"]["title"]["tag"] == "plain_text"
    assert isinstance(card["elements"], list) and card["elements"]


@pytest.mark.parametrize("sev,template", [("crit", "red"), ("warn", "orange"),
                                          ("info", "blue")])
def test_severity_maps_to_header_color(ivyea_home, sev, template):
    from ivyea_agent import feishu_card as fc

    assert fc.build_finding_card(_finding(severity=sev), "ap1")["header"]["template"] == template


# ── 按钮契约（relay 依赖它，改了必须同步）──────────────────────────────────
def test_button_value_contract(ivyea_home):
    from ivyea_agent import feishu_card as fc

    card = fc.build_finding_card(_finding(), "ap-xyz")
    actions = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]
    values = [b["value"] for b in actions]
    assert {"ivyea_action": "approve", "approval_id": "ap-xyz"} in values
    assert {"ivyea_action": "deny", "approval_id": "ap-xyz"} in values


def test_parse_action_value_roundtrip(ivyea_home):
    from ivyea_agent import feishu_card as fc

    card = fc.build_finding_card(_finding(), "ap-xyz")
    btn = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"][0]
    assert fc.parse_action_value(btn["value"]) == ("approve", "ap-xyz")


@pytest.mark.parametrize("bad", [None, "approve", {}, {"ivyea_action": "drop_table"},
                                 {"approval_id": "x"}])
def test_parse_action_value_rejects_garbage_without_raising(ivyea_home, bad):
    """回调路径上抛异常会让飞书一直重投。非法输入必须安静地返回空。"""
    from ivyea_agent import feishu_card as fc

    assert fc.parse_action_value(bad) == ("", "")


# ── 没有可执行动作时不许出现批准按钮 ────────────────────────────────────────
def test_advisory_finding_has_no_approve_button(ivyea_home):
    """放了按钮会让人点完以为处理了，实际什么都没发生。"""
    from ivyea_agent import feishu_card as fc

    card = fc.build_finding_card(_finding(intent=None), approval_id="")
    assert not [e for e in card["elements"] if e.get("tag") == "action"]
    assert "无可自动执行的动作" in _dump(card)


def test_structural_action_warns_about_irreversibility(ivyea_home):
    from ivyea_agent import feishu_card as fc, store_health

    card = fc.build_finding_card(_finding(action_class=store_health.STRUCTURAL), "ap1")
    assert "不可逆" in _dump(card)


def test_stanch_action_mentions_rollback(ivyea_home):
    from ivyea_agent import feishu_card as fc

    assert "回滚" in _dump(fc.build_finding_card(_finding(), "ap1"))


# ── 脱敏（递归 + 数据层）────────────────────────────────────────────────────
def test_secret_by_key_is_redacted_despite_fullwidth_colon(ivyea_home):
    """证据区用全角冒号渲染，而文本脱敏正则只认半角 [:=]。
    先字符串化再脱敏会漏 —— 必须在数据层按 key 脱敏。"""
    from ivyea_agent import feishu_card as fc

    card = fc.build_finding_card(
        _finding(evidence={"api_key": "short-secret", "token": "abc123",
                           "spend": 1.0}), "ap1")
    dumped = _dump(card)
    assert "short-secret" not in dumped and "abc123" not in dumped
    assert "REDACTED" in dumped


def test_secret_by_pattern_is_redacted(ivyea_home):
    from ivyea_agent import feishu_card as fc

    card = fc.build_finding_card(
        _finding(evidence={"note": "sk-abcdefghijklmnopqrstuvwxyz"}), "ap1")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in _dump(card)


def test_secret_in_message_is_redacted(ivyea_home):
    from ivyea_agent import feishu_card as fc

    card = fc.build_finding_card(_finding(message="token=abc123xyz 泄漏了"), "ap1")
    assert "abc123xyz" not in _dump(card)


def test_nested_evidence_is_redacted(ivyea_home):
    """卡片是嵌套 JSON，只处理顶层的话深层凭据照样发到群里。"""
    from ivyea_agent import feishu_card as fc

    card = fc.build_finding_card(
        _finding(evidence={"outer": {"inner": {"password": "hunter2"}}}), "ap1")
    assert "hunter2" not in _dump(card)


# ── 证据可见性 ──────────────────────────────────────────────────────────────
def test_evidence_is_rendered_for_verification(ivyea_home):
    from ivyea_agent import feishu_card as fc

    dumped = _dump(fc.build_finding_card(_finding(), "ap1"))
    assert "spend_delta" in dumped and "890" in dumped
    assert "日内采样" in dumped          # 溯源必须可见


def test_evidence_is_truncated_with_notice(ivyea_home):
    from ivyea_agent import feishu_card as fc

    ev = {f"k{i}": i for i in range(20)}
    dumped = _dump(fc.build_finding_card(_finding(evidence=ev), "ap1"))
    assert "另有" in dumped


# ── 批量卡片 ────────────────────────────────────────────────────────────────
def test_alert_card_takes_worst_severity(ivyea_home):
    from ivyea_agent import feishu_card as fc

    card = fc.build_alert_card([_finding(severity="info"), _finding(severity="crit")],
                               sid=1, layer="L1")
    assert card["header"]["template"] == "red"


def test_alert_card_truncates_and_says_so(ivyea_home):
    from ivyea_agent import feishu_card as fc

    card = fc.build_alert_card([_finding(target_id=f"C{i}") for i in range(20)],
                               sid=1, max_items=5)
    assert "另有 15 条" in _dump(card)


def test_alert_card_empty(ivyea_home):
    from ivyea_agent import feishu_card as fc

    assert "未发现异常" in _dump(fc.build_alert_card([], sid=1))


def test_alert_card_button_count_capped(ivyea_home):
    """飞书单行按钮不宜过多，超出的只在详情里处理。"""
    from ivyea_agent import feishu_card as fc

    ids = {f"C{i}|ads.spend_burst": f"ap{i}" for i in range(10)}
    card = fc.build_alert_card([_finding(target_id=f"C{i}") for i in range(10)],
                               sid=1, approval_ids=ids)
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions and len(actions[0]["actions"]) <= 5


# ── 早报卡片 ────────────────────────────────────────────────────────────────
def test_daily_card_sections(ivyea_home):
    from ivyea_agent import feishu_card as fc

    card = fc.build_daily_card(
        date="2026-08-22", store_name="UK 店",
        metrics_lines=["销售额 ¥12,345 (▲8%)", "ACOS 17.0%"],
        findings=[_finding(), _finding(intent=None, code="sales.drop",
                                       message="ASIN B01 销量腰斩")],
        gaps=["profit.asin 无数据"],
        approval_ids={"C1|ads.spend_burst": "ap1"})
    dumped = _dump(card)
    assert "销售额" in dumped
    assert "待你决定" in dumped and "异常" in dumped
    assert "数据缺口" in dumped and "profit.asin 无数据" in dumped
    assert "ap1" in dumped


def test_daily_card_with_no_data(ivyea_home):
    from ivyea_agent import feishu_card as fc

    assert "昨日无数据" in _dump(fc.build_daily_card(
        date="2026-08-22", store_name="UK", metrics_lines=[]))


# ── 状态流转卡片 ────────────────────────────────────────────────────────────
def test_resolved_executed_failed_rolledback_cards(ivyea_home):
    from ivyea_agent import feishu_card as fc

    assert "执行中" in _dump(fc.build_resolved_card(choice="approve", operator="张三"))
    assert "已忽略" in _dump(fc.build_resolved_card(choice="deny", operator="张三"))

    ex = fc.build_executed_card(preview="预算 100 → 85", operator="张三",
                                audit_id="aud1", approval_id="ap1")
    assert ex["header"]["template"] == "green"
    rollback = [e for e in ex["elements"] if e.get("tag") == "action"][0]["actions"][0]
    assert fc.parse_action_value(rollback["value"]) == ("rollback", "ap1")

    fail = fc.build_failed_card(preview="预算 100 → 85", reason="领星写入失败")
    assert fail["header"]["template"] == "red" and "领星写入失败" in _dump(fail)

    rb = fc.build_rolled_back_card(preview="预算恢复 85 → 100", operator="张三")
    assert "已回滚" in _dump(rb)


def test_executed_card_without_audit_has_no_rollback_button(ivyea_home):
    """没有审计号就无从回滚，按钮不能给——点了会失败。"""
    from ivyea_agent import feishu_card as fc

    card = fc.build_executed_card(preview="x", operator="张三", approval_id="ap1")
    assert not [e for e in card["elements"] if e.get("tag") == "action"]


# ── 分片 ────────────────────────────────────────────────────────────────────
def test_chunk_respects_limit_and_keeps_lines(ivyea_home):
    from ivyea_agent import feishu_card as fc

    text = "\n".join(f"第 {i} 行内容" * 20 for i in range(200))
    parts = fc.chunk(text)
    assert len(parts) > 1
    assert all(len(p) <= fc.MAX_CHUNK for p in parts)
    assert "".join(parts) == text          # 不丢字符


def test_chunk_handles_single_overlong_line(ivyea_home):
    from ivyea_agent import feishu_card as fc

    parts = fc.chunk("x" * 10000)
    assert all(len(p) <= fc.MAX_CHUNK for p in parts)
    assert "".join(parts) == "x" * 10000


def test_chunk_short_and_empty(ivyea_home):
    from ivyea_agent import feishu_card as fc

    assert fc.chunk("abc") == ["abc"]
    assert fc.chunk("") == []


# ── 版式：让人一眼抓到重点（2026-08-23 按真机截图重排）───────────────────────
# 改之前的样子：一条异常是一整句 markdown，手机上折成三行，重点（2.9 星）在句尾；
# 四行"数据来源：xxx 来源 领星 OpenAPI · 延迟约 5 分钟 · 120 行"压在一条异常上面。

def _f2(**kw):
    from ivyea_agent import store_health as sh

    base = dict(code="listing.rating_low", layer="L1", severity=sh.WARN,
                action_class=sh.ADVISORY, sid=1, scope="listing",
                target_id="B08N6VPHV1", target_name="IVY 3m by 2M SD20258 Wall Art Decor",
                metric="stars", current=2.9, baseline=3.5,
                message="评分 2.9 星（5 条评价），低于 3.5 星",
                provenance="领星 MCP · 延迟约 10 分钟 · 120 行")
    base.update(kw)
    return sh.Finding(**base)


def test_headline_says_what_happened_before_which_product(ivyea_home):
    """一屏 6 条异常若全以商品名开头，扫一眼看不出哪条是断货、哪条只是评分低。"""
    from ivyea_agent import feishu_card as fc

    line = fc._target_line(_f2())
    assert line.index("评分偏低") < line.index("IVY 3m")
    assert "listing.rating_low" not in line          # 代码留给页脚


def test_numbers_go_into_paired_fields_not_into_the_sentence(ivyea_home):
    from ivyea_agent import feishu_card as fc

    card = fc.build_alert_card([_f2()], store_name="欧洲-UK", layer="L1")
    div = next(e for e in card["elements"] if e["tag"] == "div")
    got = {f["text"]["content"].split("\n")[0].strip("*"): f["text"]["content"].split("\n")[1]
           for f in div["fields"]}
    assert got["当前"] == "2.9 星" and got["门槛"] == "3.5 星"
    assert got["对象"] == "B08N6VPHV1"


def test_threshold_rules_say_threshold_and_trend_rules_say_baseline(ivyea_home):
    """「基线 3.5 星」会被读成"上周 3.5 星"。阈值型规则必须说"门槛"。"""
    from ivyea_agent import feishu_card as fc

    assert dict(fc._finding_fields(_f2()))["门槛"] == "3.5 星"
    trend = _f2(code="sales.listing_drop", metric="avg_volume_7", current=3.0, baseline=10.0)
    assert "基线" in dict(fc._finding_fields(trend))


def test_empty_numbers_are_omitted_not_shown_as_zero(ivyea_home):
    """显示一个孤零零的 0 比不显示更误导。"""
    from ivyea_agent import feishu_card as fc

    fields = dict(fc._finding_fields(_f2(metric="", current=0.0, baseline=0.0)))
    assert "当前" not in fields and "门槛" not in fields


def test_technical_noise_lives_in_the_footnote(ivyea_home):
    """数据来源 / 跳过项 / 规则代码必须留着（"没告警"不能等于"没问题"），
    但它们是排查用的，不该压在异常上面。"""
    from ivyea_agent import feishu_card as fc

    card = fc.build_alert_card([_f2()], store_name="欧洲-UK", layer="L1",
                               skipped=2, gaps=1)
    note = next(e for e in card["elements"] if e["tag"] == "note")
    text = note["elements"][0]["content"]
    assert "listing.rating_low" in text and "领星 MCP" in text
    assert "2 条规则本次跳过" in text and "1 处数据缺口" in text
    # 延迟和行数不进页脚：会把这一行撑成两行小灰字，比不写还乱
    assert "120 行" not in text
    # 页脚必须在最后，不能挤在异常前面
    assert card["elements"][-1]["tag"] == "note"


def test_header_shows_the_breakdown_not_just_a_total(ivyea_home):
    """「紧急 2 · 注意 3」比「异常 5 条」有用得多。"""
    from ivyea_agent import feishu_card as fc, store_health as sh

    card = fc.build_alert_card(
        [_f2(), _f2(code="stock.oos", severity=sh.CRIT, target_id="M1")],
        store_name="欧洲-UK")
    assert card["header"]["title"]["content"].startswith("🚨 紧急 1 · 注意 1")


def test_critical_findings_float_to_the_top(ivyea_home):
    """排错的后果是断货排在评分偏低下面，人一眼看到的是不要紧的那条。"""
    from ivyea_agent import feishu_card as fc, store_health as sh

    card = fc.build_alert_card(
        [_f2(), _f2(code="stock.oos", severity=sh.CRIT, target_id="M1",
                    target_name="断货的那个")], store_name="UK")
    first = next(e for e in card["elements"] if e["tag"] == "div")
    assert "断货" in first["text"]["content"]
