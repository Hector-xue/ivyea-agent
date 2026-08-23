"""告警节流状态机（方案 §5.3 的第 1、2 道闸）。

守的是"告警系统失效的典型路径"：不是漏报，是刷屏。
L1 按小时跑，一条持续一周没处理的断货，不节流就是 168 张一模一样的卡；
人的反应是把群静音，然后真出事的那张也一起看不见了。
"""
from __future__ import annotations


class _F:
    def __init__(self, code="stock.oos", target="M1", severity="crit", message="断货了"):
        self.code, self.target_id, self.severity, self.message = code, target, severity, message
        self.target_name = target
        self.intent = None


def test_first_time_reports_then_goes_quiet(ivyea_home):
    from ivyea_agent import alert_state

    f = _F()
    first = alert_state.triage(1, "L1", [f])
    assert len(first.fresh) == 1 and first.ongoing == []

    again = alert_state.triage(1, "L1", [_F()])
    assert again.fresh == [] and len(again.ongoing) == 1
    assert again.should_push is False


def test_recovery_is_reported_once_then_forgotten(ivyea_home):
    """只报坏消息的系统，人无法判断问题有没有解决。"""
    from ivyea_agent import alert_state

    alert_state.triage(1, "L1", [_F()])
    gone = alert_state.triage(1, "L1", [])
    assert len(gone.resolved) == 1 and gone.resolved[0]["code"] == "stock.oos"
    # 恢复只报一次，之后这条就不存在了
    assert alert_state.triage(1, "L1", []).resolved == []


def test_data_gap_never_counts_as_recovery(ivyea_home):
    """规则没跑 ≠ 问题没了。

    数据源一挂 findings 天然为空，若照常判恢复，你会在断货最严重的那天
    收到一屏「✅ 已恢复」—— 这是这个模块最危险的一条规矩。
    """
    from ivyea_agent import alert_state

    alert_state.triage(1, "L1", [_F()])
    out = alert_state.triage(1, "L1", [], clean=False)
    assert out.resolved == []
    # 状态还在：数据回来且问题真没了，那时才报恢复
    assert alert_state.triage(1, "L1", []).resolved


def test_severity_upgrade_is_a_new_event(ivyea_home):
    """warn → crit 指纹变了，必须立刻再报一次，不能被"报过了"吃掉。"""
    from ivyea_agent import alert_state

    alert_state.triage(1, "L1", [_F(severity="warn")])
    out = alert_state.triage(1, "L1", [_F(severity="crit")])
    assert len(out.fresh) == 1


def test_crit_is_reminded_sooner_than_warn(ivyea_home):
    from ivyea_agent import alert_state

    t0 = 1_000_000.0
    alert_state.triage(1, "L1", [_F(severity="crit")], now=t0)
    alert_state.triage(1, "L1", [_F(code="ads.cpc_jump", severity="warn")], now=t0)

    # 5 小时后：crit 到点再提醒一次，warn 还在静默窗口内
    later = alert_state.triage(
        1, "L1", [_F(severity="crit"), _F(code="ads.cpc_jump", severity="warn")],
        now=t0 + 5 * 3600)
    assert [f.severity for f in later.fresh] == ["crit"]
    assert [f.severity for f in later.ongoing] == ["warn"]


def test_state_is_scoped_by_store_and_layer(ivyea_home):
    """L1 的一轮巡检不能把 L3 的告警判成已恢复，别的店同理。"""
    from ivyea_agent import alert_state

    alert_state.triage(1, "L1", [_F()])
    alert_state.triage(2, "L1", [_F()])
    alert_state.triage(1, "L3", [_F(code="sales.drop")])

    out = alert_state.triage(1, "L1", [])
    assert len(out.resolved) == 1 and out.resolved[0]["sid"] == "1"
    assert len(alert_state.active()) == 2          # 店 2 的 L1 和 店 1 的 L3 都还在


def test_active_lists_what_is_still_broken(ivyea_home):
    from ivyea_agent import alert_state

    alert_state.triage(1, "L1", [_F(), _F(code="stock.days_low", severity="warn")])
    rows = alert_state.active(sid=1)
    assert {r["code"] for r in rows} == {"stock.oos", "stock.days_low"}
    assert alert_state.forget(1) == 2 and alert_state.active() == []


def test_broken_state_file_does_not_break_patrol(ivyea_home):
    """状态文件坏了顶多多报一次，绝不能让整轮巡检崩掉。"""
    from ivyea_agent import alert_state

    alert_state._FILE.write_text("{ 这不是 json", encoding="utf-8")
    assert len(alert_state.triage(1, "L1", [_F()]).fresh) == 1
