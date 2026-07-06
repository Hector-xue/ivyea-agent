from __future__ import annotations

from pathlib import Path


def _response(text: str, *, etag: str = "") -> dict:
    headers = {"etag": etag} if etag else {}
    return {"status": 200, "text": text, "headers": headers, "url": "https://developer-docs.amazon.com/llms.txt"}


def test_official_registry_is_allowlisted_and_typed():
    from ivyea_agent import knowledge_sync

    data = knowledge_sync.registry()
    assert data["summary"]["sources"] >= 15
    assert data["summary"]["public_monitorable"] >= 10
    assert data["summary"]["authorization_required"] >= 2
    assert all(row["url"].startswith("https://") for row in data["sources"])
    assert all(row["authority_tier"] and row["evidence_class"] for row in data["sources"])


def test_sync_new_unchanged_changed_enters_review_queue(ivyea_home):
    from ivyea_agent import knowledge_sync

    first = knowledge_sync.sync(
        force=True,
        source_ids=["sp_api.llms_index"],
        fetcher=lambda source, previous: _response("# SP-API\nfirst", etag="one"),
        now=1_000,
    )
    assert first["ok"] is True
    assert first["results"][0]["status"] == "new"
    assert first["results"][0]["review_required"] is False
    snapshot = Path(first["results"][0]["snapshot"])
    assert snapshot.exists()
    assert knowledge_sync.changes()["changes"] == []

    unchanged = knowledge_sync.sync(
        force=True,
        source_ids=["sp_api.llms_index"],
        fetcher=lambda source, previous: _response("# SP-API\nfirst", etag="one"),
        now=2_000,
    )
    assert unchanged["results"][0]["status"] == "unchanged"

    changed = knowledge_sync.sync(
        force=True,
        source_ids=["sp_api.llms_index"],
        fetcher=lambda source, previous: _response("# SP-API\nsecond", etag="two"),
        now=3_000,
    )
    row = changed["results"][0]
    assert row["status"] == "changed"
    assert row["review_required"] is True
    assert "-first" in row["diff"] and "+second" in row["diff"]
    queued = knowledge_sync.changes()
    assert queued["review_required"] is True
    assert queued["changes"][0]["id"] == "sp_api.llms_index"
    assert changed["publication"] == "review_required_before_import"


def test_sync_respects_cadence_and_authenticated_sources(ivyea_home):
    from ivyea_agent import knowledge_sync

    calls = []

    def fetch(source, previous):
        calls.append(source["id"])
        return _response("baseline")

    knowledge_sync.sync(
        force=True, source_ids=["sp_api.llms_index"], fetcher=fetch, now=10_000,
    )
    later = knowledge_sync.sync(
        source_ids=["sp_api.llms_index"], fetcher=fetch, now=10_001,
    )
    assert later["results"][0]["status"] == "not_due"
    assert calls == ["sp_api.llms_index"]

    private = knowledge_sync.sync(
        force=True, source_ids=["seller.help_us"], fetcher=fetch, now=20_000,
    )
    assert private["results"][0]["status"] == "authorization_required"
    assert private["results"][0]["reason"] == "authorized_export_only"
    assert calls == ["sp_api.llms_index"]


def test_rss_normalization_extracts_reviewable_items():
    from ivyea_agent import knowledge_sync

    rss = """<rss><channel><item><title>Release</title><link>https://developer-docs.amazon.com/x</link>
    <pubDate>Sun, 05 Jul 2026 00:00:00 GMT</pubDate><description><![CDATA[<b>Changed</b> schema]]></description>
    </item></channel></rss>"""
    text = knowledge_sync.normalize_content(rss, "rss")
    assert "title: Release" in text
    assert "description: Changed schema" in text
    assert "<b>" not in text


def test_unknown_sync_source_is_rejected(ivyea_home):
    import pytest
    from ivyea_agent import knowledge_sync

    with pytest.raises(ValueError, match="unknown knowledge source"):
        knowledge_sync.sync(force=True, source_ids=["not.amazon"])
