"""上下文压缩 + 会话持久化。"""
from __future__ import annotations


class _FakeProvider:
    def __init__(self, summary="要点：ASIN B0X 否词3个；用户偏好保守。"):
        self.summary = summary
        self.seen = None
    def complete(self, system, user, **k):
        self.seen = (system, user)
        return self.summary


def test_should_compact_threshold(ivyea_home):
    from ivyea_agent import config, context
    assert context.DEFAULT_AUTO_COMPACT is True           # 默认主动压缩（对标 Claude）
    config.set_setting("auto_compact", False)
    assert context.should_compact(120000) is False        # 关时不自动压
    assert context.should_warn_compact(120000) is True    # 但仍会提示手动 /compact
    config.set_setting("auto_compact", True)
    assert context.should_compact(120000) is True         # 开时越过阈值即压
    assert context.should_compact(100) is False           # 未过阈值不压


def test_compact_replaces_history_no_tool_pairs(ivyea_home):
    from ivyea_agent import context
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "看下 B0X"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "run_patrol", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "巡检完成"},
        {"role": "assistant", "content": "有3个否词候选"},
        {"role": "user", "content": "执行吧"},
    ]
    prov = _FakeProvider()
    new, summary = context.compact(messages, prov, keep_recent=0)   # 0=全量摘要（旧行为）
    assert summary
    assert new[0]["role"] == "system"
    assert "摘要" in new[1]["content"]
    # 关键：压缩后不再含 tool 消息 / tool_calls（避免 OpenAI 配对错误）
    assert not any(m.get("role") == "tool" for m in new)
    assert not any(m.get("tool_calls") for m in new)


def test_compact_too_short_noop(ivyea_home):
    from ivyea_agent import context
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    new, summary = context.compact(messages, _FakeProvider())
    assert new == messages and summary == ""


def _tool_call_msg(cid: str) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}


def test_compact_keeps_recent_verbatim_and_pair_safe(ivyea_home):
    """保留最近 N 条原文；naive 切点落在 tool 上时回退到其 assistant(tool_calls)。"""
    from ivyea_agent import context
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "旧1"},
        {"role": "assistant", "content": "旧2"},
        {"role": "user", "content": "旧3"},
        {"role": "assistant", "content": "旧4"},
        _tool_call_msg("t1"),                                   # ← 回退应停在这
        {"role": "tool", "tool_call_id": "t1", "content": "结果A"},
        {"role": "tool", "tool_call_id": "t1", "content": "结果B"},
        {"role": "assistant", "content": "近1"},
        {"role": "user", "content": "近2"},
    ]
    prov = _FakeProvider()
    # history 共 9 条，keep_recent=3 的 naive 切点是第二个 tool → 必须回退到 tool_calls
    new, summary = context.compact(messages, prov, keep_recent=3)
    assert summary
    # 保留段以 assistant(tool_calls) 开头，配对完整
    kept = new[3:]                       # [system, 摘要user, 确认assistant] 之后
    assert kept[0].get("tool_calls")
    assert [m.get("role") for m in kept] == ["assistant", "tool", "tool", "assistant", "user"]
    assert kept == messages[5:]          # 逐字保留原文
    # 摘要段（旧1..旧4）不含 tool 残留：每条 tool 前都有它的 assistant.tool_calls
    for i, m in enumerate(new):
        if m.get("role") == "tool":
            prev = new[i - 1]
            assert prev.get("tool_calls") or prev.get("role") == "tool"


def test_compact_keep_recent_from_config(ivyea_home):
    """默认参数读 compact_keep_recent 配置键。"""
    from ivyea_agent import config, context
    config.set_setting("compact_keep_recent", 2)
    messages = [{"role": "system", "content": "s"}] + \
        [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(8)]
    new, summary = context.compact(messages, _FakeProvider())
    assert summary
    assert new[-2:] == messages[-2:]     # 保留最近 2 条原文


def test_compact_short_history_falls_back_to_full_summary(ivyea_home):
    """扣掉保留段后可压部分不足 4 条、但历史本身够长（防溢出场景）→ 退回全量摘要。"""
    from ivyea_agent import context
    messages = [{"role": "system", "content": "s"}] + \
        [{"role": "user", "content": f"m{i}"} for i in range(5)]
    new, summary = context.compact(messages, _FakeProvider(), keep_recent=3)
    assert summary
    assert len(new) == 3                 # system + 摘要 + 确认（无保留段）


def test_compact_provider_failure_returns_original(ivyea_home):
    from ivyea_agent import context

    class _Boom:
        def complete(self, *a, **k):
            raise RuntimeError("llm down")

    messages = [{"role": "system", "content": "s"}] + \
        [{"role": "user", "content": f"m{i}"} for i in range(10)]
    new, summary = context.compact(messages, _Boom(), keep_recent=2)
    assert new == messages and summary == ""


def test_sessions_save_load_latest(ivyea_home):
    from ivyea_agent import sessions
    sid = sessions.new_id()
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "看广告"}]
    sessions.save(sid, msgs, model="deepseek-chat", usage={"cost": 0.01, "turns": 1})
    loaded = sessions.load(sid)
    assert loaded["messages"] == msgs and loaded["model"] == "deepseek-chat"
    assert sessions.latest_id() == sid
    lst = sessions.listing()
    assert lst and lst[0]["id"] == sid and "看广告" in lst[0]["preview"]


def test_sessions_new_id_is_unique(ivyea_home):
    from ivyea_agent import sessions

    ids = {sessions.new_id() for _ in range(20)}
    assert len(ids) == 20


def test_sessions_load_missing(ivyea_home):
    from ivyea_agent import sessions
    assert sessions.load("nope-does-not-exist") is None


# ── 会话 id 是不可信输入 ────────────────────────────────────────────────────
# session_id 直接拼进文件名，而它是**调用方给的**（serve 的 payload.session_id、
# 导入接口的 id）。曾实测可打通：往 /v1/chat/sessions POST 一个
# id="../../../tmp/PWNED"，daemon（常以 root 跑）就在会话目录之外写出了文件。

def test_path_for_rejects_traversal(tmp_path, monkeypatch):
    import pytest

    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    for bad in ["../../../../tmp/PWNED", "/tmp/PWNED", "..", "a/b", "a\\b", "", "x" * 200]:
        with pytest.raises(ValueError):
            sessions.path_for(bad)


def test_path_for_accepts_real_ids(tmp_path, monkeypatch):
    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    # new_id() 的产物、以及更老的不带随机后缀的历史 id，都必须继续可用
    assert sessions.path_for(sessions.new_id()).parent == tmp_path
    assert sessions.path_for("20260617-120252").name == "20260617-120252.json"
    assert sessions.path_for("imp-assistant-1743001").name == "imp-assistant-1743001.json"


def test_save_refuses_to_write_outside_the_sessions_dir(tmp_path, monkeypatch):
    import pytest

    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    with pytest.raises(ValueError):
        sessions.save("../escaped", [{"role": "user", "content": "x"}])
    assert not (tmp_path.parent / "escaped.json").exists()


def test_load_and_delete_treat_bad_ids_as_missing(tmp_path, monkeypatch):
    """查询语义：非法 id 等同"查无此会话"，不该把异常甩给调用方。"""
    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    assert sessions.load("../../etc/passwd") is None
    assert sessions.delete("../../etc/passwd") is False


# ── 并发落盘 ────────────────────────────────────────────────────────────────
# 一轮的流程是"开始读全部历史 → 跑 → 结束写回全部"。两个标签页同时在一条会话里
# 发消息，各自读到那一刻的历史，结束时各自写回自己那份 —— 后写的赢，先写的整轮
# （连问带答）就没了，而且**没有任何报错**。实测复现过。

def test_append_turn_keeps_both_concurrent_turns(tmp_path, monkeypatch):
    import threading

    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    sid = "20260808-000000-000-abcd"
    sessions.save(sid, [{"role": "system", "content": "sys"},
                        {"role": "user", "content": "第一轮"},
                        {"role": "assistant", "content": "答一"}])

    start = threading.Barrier(2)

    def turn(tag):
        # 模拟真实流程：先读历史，再（并发地）写回自己这一轮
        base = sessions.load(sid)["messages"]
        start.wait()
        sessions.append_turn(sid, "sys", [
            {"role": "user", "content": f"问-{tag}"},
            {"role": "assistant", "content": f"答-{tag}"},
        ])
        return len(base)

    threads = [threading.Thread(target=turn, args=(t,)) for t in ("甲", "乙")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    msgs = sessions.load(sid)["messages"]
    users = [m["content"] for m in msgs if m["role"] == "user"]
    # 顺序按落盘先后交错，但**一条都不能少**
    assert sorted(users) == sorted(["第一轮", "问-甲", "问-乙"]), users
    assert len([m for m in msgs if m["role"] == "assistant"]) == 3
    assert msgs[0]["role"] == "system" and len(msgs) == 7


def test_append_turn_refreshes_the_system_prompt(tmp_path, monkeypatch):
    """system 是这一轮的运行时上下文（带着当前技能/知识注入），要用新的那份。"""
    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    sid = "20260808-000000-000-abce"
    sessions.save(sid, [{"role": "system", "content": "旧"},
                        {"role": "user", "content": "上一轮"}])
    sessions.append_turn(sid, "新", [{"role": "user", "content": "这一轮"}])
    msgs = sessions.load(sid)["messages"]
    assert msgs[0] == {"role": "system", "content": "新"}
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["上一轮", "这一轮"]


def test_append_turn_creates_the_session_when_absent(tmp_path, monkeypatch):
    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    sid = "20260808-000000-000-abcf"
    sessions.append_turn(sid, "sys", [{"role": "user", "content": "第一句"}])
    msgs = sessions.load(sid)["messages"]
    assert msgs[0]["role"] == "system" and msgs[1]["content"] == "第一句"


def test_append_turn_preserves_the_original_created_time(tmp_path, monkeypatch):
    """创建时间是会话的身份之一，后续轮次不该把它刷成"刚刚"。"""
    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    sid = "20260808-000000-000-abd0"
    sessions.save(sid, [{"role": "user", "content": "x"}], created=1000.0)
    sessions.append_turn(sid, "sys", [{"role": "user", "content": "y"}], created=9999.0)
    assert sessions.load(sid)["created"] == 1000.0


def test_windows_reserved_device_names_are_rejected(tmp_path, monkeypatch):
    """`NUL.json` 在 Windows 上**就是空设备**：会话写进去内容直接消失，还不报错。
    `CON` 会去开控制台。这些全是合法字符，字符集守卫拦不住。

    无论当前跑在哪个系统都要拒 —— 会话文件会跟着备份/同步挪到 Windows 机器上，
    daemon 本身也支持 Windows。只在 nt 上拦，等于放任生成一批到了 Windows 才炸的 id。
    """
    import pytest

    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    for name in ["NUL", "CON", "PRN", "AUX", "COM1", "LPT9", "nul", "Con", "com1"]:
        assert sessions.is_safe_id(name) is False, name
        with pytest.raises(ValueError):
            sessions.path_for(name)


def test_only_exact_device_names_are_reserved(tmp_path, monkeypatch):
    """别误伤：只有**整个 id 等于**设备名才算，前缀像的不算。"""
    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    for name in ["CONSOLE", "NULL", "com10", "COM", "LPT", "nul-1", "imp-brain-con"]:
        assert sessions.is_safe_id(name) is True, name


def test_temp_file_name_is_unique_per_writer(tmp_path, monkeypatch):
    """临时文件名不能是固定的 `<id>.json.tmp`。

    两个**进程**同时写同一条会话（工作台的 serve + 一个 `ivyea chat`）会写进同一个
    临时文件，互相踩出半截 JSON —— 进程内的会话锁管不到跨进程。
    """
    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    seen = []
    real_write = sessions.Path.write_text

    def spy(self, *a, **k):
        if self.suffix == ".tmp":
            seen.append(self.name)
        return real_write(self, *a, **k)

    monkeypatch.setattr(sessions.Path, "write_text", spy)
    for _ in range(3):
        sessions.save("20260808-000000-000-aaaa", [{"role": "user", "content": "x"}])
    assert len(set(seen)) == 3, seen                       # 每次都不同
    assert not list(tmp_path.glob("*.tmp"))                # 也没留下垃圾


def test_save_retries_when_windows_holds_the_target_open(tmp_path, monkeypatch):
    """Windows 上 os.replace 会在别的进程正开着目标文件时抛 PermissionError
    （POSIX 从不会）。目标恰恰是会被并发读的会话文件，而 Windows 是主要用户环境
    —— 不重试的话，赶上一次就是这一轮的回答没落盘。"""
    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions.time, "sleep", lambda _s: None)
    calls = {"n": 0}
    real_replace = sessions.Path.replace

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "被占用")
        return real_replace(self, target)

    monkeypatch.setattr(sessions.Path, "replace", flaky)
    sessions.save("20260808-000000-000-aaaa", [{"role": "user", "content": "撑过去了"}])
    assert calls["n"] == 3
    assert sessions.load("20260808-000000-000-aaaa")["messages"][0]["content"] == "撑过去了"


def test_save_gives_up_cleanly_if_the_file_stays_locked(tmp_path, monkeypatch):
    """一直占着就得抛，但别把半截临时文件留在会话目录里。"""
    import pytest

    from ivyea_agent import sessions

    monkeypatch.setattr(sessions, "_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sessions.Path, "replace",
                        lambda self, t: (_ for _ in ()).throw(PermissionError(13, "一直被占用")))
    with pytest.raises(PermissionError):
        sessions.save("20260808-000000-000-aaaa", [{"role": "user", "content": "x"}])
    assert not list(tmp_path.glob("*.tmp"))
