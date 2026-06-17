"""影子模式：台账记录/去重、回测数学、模式开关、写入门控。"""
from __future__ import annotations


def _cand(lever, target, *, clicks=0, spend=0.0, orders=0, acos=None, cid="C1"):
    return {"lever": lever, "target_name": target, "campaign_id": cid,
            "metrics": {"clicks": clicks, "spend": spend, "orders": orders, "acos": acos},
            "rule": f"{lever}「{target}」"}


def test_record_and_dedup(ivyea_home):
    from ivyea_agent import shadow
    cands = [_cand("否词", "junk a", clicks=20, spend=15),
             _cand("收割", "winner b", orders=5)]
    assert shadow.record(1876, cands) == 2
    assert shadow.record(1876, cands) == 0          # 7天内同 sid+lever+target 不重复
    assert len(shadow.list_recs("1876")) == 2


def test_record_skips_blocked_and_errors(ivyea_home):
    from ivyea_agent import shadow
    cands = [_cand("错误", "x"), {"lever": "否词"}]   # 错误/无 target 跳过
    assert shadow.record(1, cands) == 0


def test_evaluate_negative_saving(ivyea_home):
    from ivyea_agent import shadow
    recs = [{"lever": "否词", "target": "junk a"}, {"lever": "否词", "target": "junk b"}]
    # junk a 之后又烧 ¥12 仍 0 单 → 该省；junk b 之后出了单 → 不算纯浪费
    current = {"junk a": {"spend": 12.0, "orders": 0}, "junk b": {"spend": 5.0, "orders": 2}}
    r = shadow.evaluate(recs, current)
    assert r["saved_terms"] == 1 and abs(r["saved_cny"] - 12.0) < 1e-9
    assert r["evaluated"] == 2


def test_evaluate_harvest(ivyea_home):
    from ivyea_agent import shadow
    recs = [{"lever": "收割", "target": "winner"}]
    current = {"winner": {"spend": 3.0, "orders": 7}}
    r = shadow.evaluate(recs, current)
    assert r["harvest_terms"] == 1 and r["harvested_orders"] == 7


def test_evaluate_no_followup_data(ivyea_home):
    from ivyea_agent import shadow
    recs = [{"lever": "否词", "target": "gone"}]
    r = shadow.evaluate(recs, {})        # 无后续数据 → 不计
    assert r["evaluated"] == 0 and r["saved_cny"] == 0.0


def test_shadow_mode_toggle(ivyea_home):
    from ivyea_agent import shadow
    assert shadow.shadow_mode() is False
    shadow.set_shadow(True)
    assert shadow.shadow_mode() is True


def test_shadow_mode_gates_chat_write(ivyea_home):
    from ivyea_agent import shadow, agent_tools
    shadow.set_shadow(True)
    ctx = agent_tools.ToolContext()
    ctx.lingxing_result = {"candidates": [_cand("否词", "junk", clicks=20, spend=15)]}
    out = agent_tools.dispatch("execute_actions", {}, ctx)
    assert "影子模式" in out and "只记不写" in out


def test_summary_text(ivyea_home):
    from ivyea_agent import shadow
    r = shadow.evaluate([{"lever": "否词", "target": "t"}], {"t": {"spend": 9.9, "orders": 0}})
    txt = shadow.summary_text("1876", r)
    assert "已省 ¥9.90" in txt and "信任报告" in txt
