"""快照差分测试。冷启动保护是重点：首轮一条都不能报。"""
from __future__ import annotations


def _rows(*pairs):
    return [{"id": i, "budget": b, "state": s} for i, b, s in pairs]


def test_cold_start_reports_nothing(ivyea_home):
    from ivyea_agent import snapshots

    rows = _rows(("a", 10.0, "enabled"), ("b", 20.0, "enabled"))
    d = snapshots.diff(1, "camp", rows, "id", ["budget", "state"])
    assert d.first_run is True
    assert d.changes == []
    assert not d.has_baseline


def test_diff_after_baseline(ivyea_home):
    from ivyea_agent import snapshots

    snapshots.diff(1, "camp", _rows(("a", 10.0, "enabled")), "id", ["budget"])
    snapshots.save(1, "camp", _rows(("a", 10.0, "enabled")), "id")

    d = snapshots.diff(1, "camp", _rows(("a", 3.0, "enabled")), "id", ["budget"])
    assert d.has_baseline
    assert len(d.changes) == 1
    c = d.changes[0]
    assert (c.entity_id, c.field, c.before, c.after) == ("a", "budget", 10.0, 3.0)


def test_membership_changes(ivyea_home):
    from ivyea_agent import snapshots

    snapshots.diff(1, "camp", _rows(("a", 1.0, "enabled")), "id", ["budget"])
    snapshots.save(1, "camp", _rows(("a", 1.0, "enabled")), "id")
    d = snapshots.diff(1, "camp", _rows(("b", 2.0, "enabled")), "id", ["budget"])
    kinds = sorted(c.change for c in d.changes)
    assert kinds == ["added", "removed"]


def test_membership_can_be_disabled(ivyea_home):
    from ivyea_agent import snapshots

    snapshots.diff(1, "camp", _rows(("a", 1.0, "enabled")), "id", ["budget"])
    snapshots.save(1, "camp", _rows(("a", 1.0, "enabled")), "id")
    d = snapshots.diff(1, "camp", _rows(("b", 2.0, "enabled")), "id", ["budget"],
                       track_membership=False)
    assert d.changes == []


def test_diff_does_not_write(ivyea_home):
    """diff 必须是纯读——写入由 save 显式做，否则失败重试会吞掉一次变更。"""
    from ivyea_agent import snapshots

    snapshots.diff(1, "camp", _rows(("a", 1.0, "enabled")), "id", ["budget"])
    assert not snapshots.has_baseline(1, "camp")


def test_scopes_are_isolated_by_sid_and_kind(ivyea_home):
    from ivyea_agent import snapshots

    snapshots.save(1, "camp", _rows(("a", 1.0, "enabled")), "id")
    assert snapshots.has_baseline(1, "camp")
    assert not snapshots.has_baseline(2, "camp")
    assert not snapshots.has_baseline(1, "kw")


def test_save_replaces_whole_scope(ivyea_home):
    from ivyea_agent import snapshots

    snapshots.save(1, "camp", _rows(("a", 1.0, "e"), ("b", 2.0, "e")), "id")
    snapshots.save(1, "camp", _rows(("a", 1.0, "e")), "id")
    assert set(snapshots.load(1, "camp")) == {"a"}


def test_rows_without_key_are_skipped(ivyea_home):
    from ivyea_agent import snapshots

    n = snapshots.save(1, "camp", [{"id": "", "budget": 1.0}, {"budget": 2.0}], "id")
    assert n == 0


def test_clear(ivyea_home):
    from ivyea_agent import snapshots

    snapshots.save(1, "camp", _rows(("a", 1.0, "e")), "id")
    snapshots.clear(1, "camp")
    assert not snapshots.has_baseline(1, "camp")
    assert snapshots.load(1, "camp") == {}
