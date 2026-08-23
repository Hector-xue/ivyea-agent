"""relay 三道闸测试。安全攸关，不依赖飞书连接。"""



from ivyea_agent.feishu_relay import gates


def test_empty_whitelist_denies_everyone():
    """空集合当"放行所有"是危险默认：忘配一次 = 把改预算的权限开给所有人。"""
    assert gates.sender_allowed("ou_anyone", set()) is False
    assert gates.sender_allowed("", set()) is False


def test_whitelist_exact_match():
    allowed = {"ou_me"}
    assert gates.sender_allowed("ou_me", allowed) is True
    assert gates.sender_allowed("ou_other", allowed) is False
    assert gates.sender_allowed("", allowed) is False


def test_chat_whitelist_empty_means_unrestricted():
    """会话最终一致性由 agent 按 approval 记录判定，relay 这层留空即不额外限制。"""
    assert gates.chat_allowed("oc_any", set()) is True
    assert gates.chat_allowed("oc_a", {"oc_a"}) is True
    assert gates.chat_allowed("oc_b", {"oc_a"}) is False


def test_dedup_first_then_replay():
    d = gates.TokenDedup(ttl=60)
    assert d.seen("t1") is False
    assert d.seen("t1") is True
    assert d.seen("t2") is False


def test_dedup_empty_token_never_dedups():
    """没有 token 时不能把不同的点击误判成同一次。"""
    d = gates.TokenDedup(ttl=60)
    assert d.seen("") is False
    assert d.seen("") is False


def test_dedup_expires():
    d = gates.TokenDedup(ttl=60)
    d.seen("t1", now=1000.0)
    assert d.seen("t1", now=1000.0 + 30) is True
    assert d.seen("t1", now=1000.0 + 61) is False


def test_dedup_capacity_evicts_oldest():
    d = gates.TokenDedup(ttl=10_000, cap=5)
    for i in range(20):
        d.seen(f"t{i}", now=1000.0 + i)
    assert len(d) <= 5
