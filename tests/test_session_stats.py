"""会话累计账 + 实时计划 + 待审批对账 —— 三件都是"界面上看得见"的契约。

会话累计账（sessions.stats）：此前每轮的用时/用量只在 SSE 流里飘过一次，前端记在
内存里。刷新、换台机器、隔天回来打开同一条会话，统计条上就只剩"几轮几步" ——
用户问的"这条会话花了多少时间、多少 token"答不出来。所以它必须落盘。

一条贯穿的规矩：**测不到的项不补 0**。补 0 等于替 provider 断言"这轮没花"，
而真相是"这个版本/这条链路没回报"。
"""
import importlib
import sys


def _mods():
    """conftest 的 ivyea_home 会重载 sessions —— 必须重新取那一份，
    否则写进去的是旧模块记着的老目录（也就是真实 ~/.ivyea）。"""
    sessions = importlib.reload(sys.modules["ivyea_agent.sessions"]) \
        if "ivyea_agent.sessions" in sys.modules else importlib.import_module("ivyea_agent.sessions")
    service = importlib.import_module("ivyea_agent.service")
    service.sessions = sessions
    return sessions, service


def test_append_turn_accumulates_across_turns(ivyea_home):
    sessions, service = _mods()
    sid = "20260820-000000-000-test"

    sessions.append_turn(
        sid, "sys", [{"role": "user", "content": "一"}, {"role": "assistant", "content": "1"}],
        model="m", turn_stat={"ms": 1200, "steps": 3,
                              "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                        "llm_ms": 800}})
    sessions.append_turn(
        sid, "sys", [{"role": "user", "content": "二"}, {"role": "assistant", "content": "2"}],
        model="m", turn_stat={"ms": 800, "steps": 1,
                              "usage": {"prompt_tokens": 150, "completion_tokens": 30,
                                        "llm_ms": 400}})

    stats = (sessions.load(sid) or {}).get("stats") or {}
    assert stats["turns"] == 2
    assert stats["steps"] == 4
    assert stats["elapsed_ms"] == 2000
    assert stats["usage"] == {"prompt_tokens": 250, "completion_tokens": 50, "llm_ms": 1200}


def test_stats_survive_a_plain_save(ivyea_home):
    """save() 是整份覆盖语义（CLI 每轮就这么写），但它不该顺手把累计账抹掉 ——
    和 steps/skill_matches 同一条理由：不知道 ≠ 要清空。"""
    sessions, service = _mods()
    sid = "20260820-000000-001-test"
    sessions.append_turn(sid, "sys", [{"role": "user", "content": "一"}],
                         turn_stat={"ms": 500, "steps": 2, "usage": {"prompt_tokens": 9}})
    sessions.save(sid, [{"role": "user", "content": "一"}], model="m")
    stats = (sessions.load(sid) or {}).get("stats") or {}
    assert stats["turns"] == 1 and stats["elapsed_ms"] == 500


def test_missing_measurements_are_not_counted_as_zero(ivyea_home):
    sessions, service = _mods()
    sid = "20260820-000000-002-test"
    # 老 provider：一个数都不回报。轮数照数，别的项一个都不该凭空长出来。
    sessions.append_turn(sid, "sys", [{"role": "user", "content": "一"}],
                         turn_stat={"usage": {}})
    stats = (sessions.load(sid) or {}).get("stats") or {}
    assert stats["turns"] == 1
    assert "elapsed_ms" not in stats and "steps" not in stats and "usage" not in stats


def test_detail_exposes_stats(ivyea_home):
    sessions, service = _mods()
    sid = "20260820-000000-003-test"
    sessions.append_turn(sid, "sys",
                         [{"role": "user", "content": "一"}, {"role": "assistant", "content": "1"}],
                         turn_stat={"ms": 700, "steps": 1, "usage": {"completion_tokens": 5}})
    detail = service.chat_session_detail(sid)["session"]
    # 累计账按**整份存档**算，不跟着分页缩水 —— 用户问的从来不是"这一页花了多少"。
    assert detail["stats"]["turns"] == 1
    assert detail["stats"]["elapsed_ms"] == 700


def test_pending_permissions_endpoint_shape():
    """待审批对账：ops 那张表是流水账，只有这里说的才算数。空 = 都不在等了。"""
    _, service = _mods()
    out = service.pending_permissions_state()
    assert out["ok"] is True
    assert isinstance(out["pending"], list)
