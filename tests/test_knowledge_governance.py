from __future__ import annotations

import json
import hashlib
import hmac
from pathlib import Path
import time
from types import SimpleNamespace

import pytest


def _response(text: str, etag: str) -> dict:
    return {
        "status": 200,
        "text": text,
        "headers": {"etag": etag},
        "url": "https://developer-docs.amazon.com/llms.txt",
    }


def _changed_event(knowledge_sync) -> dict:
    knowledge_sync.sync(
        force=True,
        source_ids=["sp_api.llms_index"],
        fetcher=lambda source, previous: _response("baseline", "one"),
        now=1_000,
    )
    changed = knowledge_sync.sync(
        force=True,
        source_ids=["sp_api.llms_index"],
        fetcher=lambda source, previous: _response("changed content", "two"),
        now=2_000,
    )
    return changed["results"][0]


def test_change_review_requires_confirmation_and_never_publishes(ivyea_home):
    from ivyea_agent import knowledge_sync

    changed = _changed_event(knowledge_sync)
    assert changed["event_id"].startswith("chg-")
    queue = knowledge_sync.changes()
    assert queue["summary"]["pending"] == 1
    assert queue["changes"][0]["review_status"] == "pending"

    blocked = knowledge_sync.review_change(changed["event_id"], "approved", confirm=False)
    assert blocked["error"] == "confirmation_required"
    assert knowledge_sync.review_history()["reviews"] == []

    reviewed = knowledge_sync.review_change(
        changed["event_id"], "approved", reviewer="qa", reviewer_source="test_authenticated_admin",
        identity_verified=True, note="verified diff; owner@example.com", confirm=True, now=3_000,
    )
    assert reviewed["ok"] is True
    assert reviewed["ready_for_import_draft"] is True
    assert reviewed["knowledge_published"] is False
    approved = knowledge_sync.changes(review_status="approved")
    assert approved["summary"]["approved"] == 1
    assert approved["changes"][0]["ready_for_import_draft"] is True
    assert approved["changes"][0]["review_identity_verified"] is True
    assert approved["publication"].startswith("approval_only")
    history = knowledge_sync.review_history(event_id=changed["event_id"])
    assert history["reviews"][0]["decision"] == "approved"
    assert history["reviews"][0]["publication"] == "not_published"
    assert "owner@example.com" not in str(history)


def test_legacy_change_without_event_id_is_migrated_on_read(ivyea_home):
    from ivyea_agent import knowledge_sync

    changed = _changed_event(knowledge_sync)
    path = knowledge_sync._events_file()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0].pop("event_id")
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    row = knowledge_sync.changes()["changes"][0]
    assert row["event_id"] == changed["event_id"]
    assert row["review_status"] == "pending"


def test_change_review_detects_snapshot_tampering(ivyea_home):
    from ivyea_agent import knowledge_sync

    changed = _changed_event(knowledge_sync)
    Path(changed["snapshot"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity check failed"):
        knowledge_sync.review_change(changed["event_id"], "rejected", confirm=True)


def test_unverified_approved_review_cannot_prepare_publication(ivyea_home):
    from ivyea_agent import knowledge_sync

    changed = _changed_event(knowledge_sync)
    reviewed = knowledge_sync.review_change(
        changed["event_id"], "approved", reviewer="asserted-user", confirm=True,
    )
    assert reviewed["ready_for_import_draft"] is False
    assert knowledge_sync.changes()["summary"]["unverified_approved"] == 1
    with pytest.raises(ValueError, match="verified reviewer identity"):
        knowledge_sync.change_packet(changed["event_id"])


def test_ops_reviewer_identity_assertion_requires_valid_hmac_and_fresh_timestamp():
    from ivyea_agent.service import _Handler

    token = "shared-test-token"
    timestamp = str(int(time.time()))
    body = {
        "event_id": "chg-1",
        "decision": "approved",
        "reviewer": "admin@example.com",
        "reviewer_source": "ops_authenticated_admin",
    }
    material = "|".join([body["event_id"], body["decision"], body["reviewer"], timestamp])
    signature = hmac.new(token.encode(), material.encode(), hashlib.sha256).hexdigest()
    fake = SimpleNamespace(server=SimpleNamespace(api_token=token))
    verified = _Handler._verified_review_payload(fake, {
        **body, "identity_assertion": {"timestamp": timestamp, "signature": signature},
    })
    assert verified["identity_verified"] is True
    assert "identity_assertion" not in verified

    tampered = _Handler._verified_review_payload(fake, {
        **body, "reviewer": "forged@example.com",
        "identity_assertion": {"timestamp": timestamp, "signature": signature},
    })
    assert tampered["identity_verified"] is False


def test_approved_change_prepares_and_separately_applies_evidence_linked_draft(ivyea_home):
    from ivyea_agent import knowledge, knowledge_sync

    changed = _changed_event(knowledge_sync)
    knowledge_sync.review_change(
        changed["event_id"], "approved", reviewer_source="test_authenticated_admin",
        identity_verified=True, confirm=True, now=3_000,
    )
    target_id = "sp_api.common_http_authorization_errors"
    packet = knowledge_sync.change_packet(changed["event_id"], card_id=target_id)
    assert packet["target"]["id"] == target_id
    assert packet["snapshot_excerpt"] == "changed content"
    assert packet["selection_required"] is False

    empty = knowledge_sync.prepare_change_draft(changed["event_id"], card_id=target_id)
    assert empty["draft_ready"] is False
    body = "# Reviewed official update\n\nThe approved source change requires a maintainer-reviewed runtime update."
    prepared = knowledge_sync.prepare_change_draft(changed["event_id"], card_id=target_id, body=body)
    assert prepared["draft_ready"] is True
    assert prepared["draft"]["card_id"].startswith("user.official_update.")
    assert changed["event_id"] in prepared["draft"]["body"]
    assert "official_change_final_confirmation_required" in prepared["draft"]["warnings"]

    blocked = knowledge_sync.apply_change_draft(
        changed["event_id"], card_id=target_id, body=body, confirm=False, rebuild_indexes=False,
    )
    assert blocked["ok"] is False
    assert blocked["result"]["error"] == "confirmation_required"
    assert knowledge_sync.publication_history()["publications"] == []

    applied = knowledge_sync.apply_change_draft(
        changed["event_id"], card_id=target_id, body=body, confirm=True, rebuild_indexes=False,
    )
    assert applied["ok"] is True
    card_id = applied["result"]["card"]["id"]
    assert knowledge.get_card(card_id)
    publication = knowledge_sync.publication_history(event_id=changed["event_id"])
    assert publication["publications"][0]["card_id"] == card_id
    queue = knowledge_sync.changes()
    assert queue["summary"]["published"] == 1
    assert queue["summary"]["approved_not_published"] == 0
    assert queue["changes"][0]["ready_for_import_draft"] is False

    duplicate = knowledge_sync.apply_change_draft(
        changed["event_id"], card_id=target_id, body=body, confirm=True, rebuild_indexes=False,
    )
    assert duplicate["error"] == "change_already_published"


def test_change_review_rejects_unknown_status_and_decision(ivyea_home):
    from ivyea_agent import knowledge_sync

    with pytest.raises(ValueError, match="review_status"):
        knowledge_sync.changes(review_status="invalid")
    with pytest.raises(ValueError, match="decision"):
        knowledge_sync.review_change("chg-none", "pending", confirm=True)


def test_governance_coverage_closes_required_marketplace_matrix(ivyea_home):
    from ivyea_agent import knowledge_governance

    data = knowledge_governance.coverage()
    assert data["summary"]["requirements"] >= 30
    assert data["summary"]["covered"] == data["summary"]["requirements"]
    assert data["summary"]["gaps"] == 0
    by_key = {(row["domain"], row["marketplace"]): row for row in data["requirements"]}
    assert by_key[("seller_registration", "US")]["status"] == "strong"
    assert by_key[("seller_registration", "JP")]["status"] == "strong"
    assert by_key[("seller_registration", "CA")]["status"] == "strong"
    assert by_key[("account_health", "UK")]["status"] == "strong"
    assert by_key[("account_health", "JP")]["status"] == "strong"
    assert by_key[("traffic_governance", "GLOBAL")]["status"] == "governed"


def test_governance_freshness_tracks_overdue_and_error_sources(ivyea_home):
    from ivyea_agent import knowledge_governance, knowledge_sync

    knowledge_sync.sync(
        force=True,
        source_ids=["sp_api.llms_index"],
        fetcher=lambda source, previous: _response("baseline", "one"),
        now=1_000,
    )
    overdue = knowledge_governance.freshness(now=1_000 + 40 * 3600)
    row = next(item for item in overdue["sources"] if item["id"] == "sp_api.llms_index")
    assert row["status"] == "overdue"

    def fail(source, previous):
        raise RuntimeError("network unavailable")

    knowledge_sync.sync(
        force=True, source_ids=["sp_api.llms_index"], fetcher=fail, now=2_000,
    )
    failed = knowledge_governance.freshness(now=2_001)
    row = next(item for item in failed["sources"] if item["id"] == "sp_api.llms_index")
    assert row["status"] == "error"
    assert failed["summary"]["monitor_status"]["error"] == 1


def test_stale_retrieval_is_downgraded_and_warned(ivyea_home, monkeypatch):
    from ivyea_agent import knowledge

    monkeypatch.setattr(knowledge, "_freshness", lambda card: "stale_needs_review")
    evidence = knowledge.evidence_context("亚马逊广告 ACoS ROAS 计算口径", limit=3)
    assert evidence["freshness_review_required"] is True
    assert "时效门禁" in evidence["text"]


def test_enhanced_conflicts_flag_unsupported_algorithm_claim(ivyea_home):
    from ivyea_agent import knowledge

    knowledge.import_text(
        "Traffic pool absolute claim",
        "提高预算一定进入更大流量池，算法权重固定提高 20%。",
        source_type="user",
        source_url="user://operator-note",
        tags=["traffic", "algorithm", "bidding"],
        card_id="user.algorithm_claim",
    )
    rows = knowledge.conflicts()
    assert any(
        row["id"] == "user.algorithm_claim" and row["reason_code"] == "unsupported_algorithm_or_numeric_claim"
        for row in rows
    )
    assert all(row.get("fingerprint") for row in rows)


def test_continuous_quality_suite_and_schedule(ivyea_home):
    from ivyea_agent import knowledge_quality, schedule

    result = knowledge_quality.run()
    assert result["ok"] is True
    assert result["summary"]["cases"] == 41
    assert result["summary"]["pass_rate"] == 1.0
    assert len(result["summary"]["domains"]) >= 8
    ok, text = schedule.run_task("knowledge_quality")
    assert ok is True
    assert "result=PASS" in text


def test_governance_cli_and_service_contracts(ivyea_home, capsys):
    from ivyea_agent import knowledge_sync, service
    from ivyea_agent.cli import main

    changed = _changed_event(knowledge_sync)
    assert main(["knowledge", "changes", "--status", "pending"]) == 0
    assert changed["event_id"] in capsys.readouterr().out
    assert main([
        "knowledge", "review", changed["event_id"], "--decision", "approved", "--confirm", "--reviewer", "qa",
    ]) == 0
    assert "knowledge_published=false" in capsys.readouterr().out
    assert main(["knowledge", "review-history", changed["event_id"]]) == 0
    assert "approved" in capsys.readouterr().out
    assert main(["knowledge", "coverage"]) == 0
    assert "coverage_rate" in capsys.readouterr().out
    assert main(["knowledge", "quality"]) == 0
    assert "result=PASS" in capsys.readouterr().out

    dashboard = service.knowledge_governance_dashboard()
    assert "coverage" in dashboard and "freshness" in dashboard
    quality = service.knowledge_quality_run()
    assert quality["ok"] is True
    manifest = service.manifest()
    assert manifest["capabilities"]["knowledge_governance_dashboard"] is True
    assert manifest["capabilities"]["knowledge_version_history"] is True
    assert any(row["path"] == "/v1/knowledge/changes/review" for row in manifest["endpoints"])
    assert any(row["path"] == "/v1/knowledge/changes/{event_id}/packet" for row in manifest["endpoints"])
    assert any(row["path"] == "/v1/knowledge/changes/draft" for row in manifest["endpoints"])
    assert any(row["path"] == "/v1/knowledge/changes/apply" for row in manifest["endpoints"])
    assert any(row["path"] == "/v1/knowledge/versions" for row in manifest["endpoints"])
    assert any(row["path"] == "/v1/knowledge/versions/rollback" for row in manifest["endpoints"])


def test_alerts_surface_review_backlog_without_closed_coverage_alert(ivyea_home):
    from ivyea_agent import alerts, knowledge_sync

    _changed_event(knowledge_sync)
    rows = alerts.check(limit=20)
    codes = {row["code"] for row in rows}
    assert "knowledge.review_backlog" in codes
    assert "knowledge.coverage_gaps" not in codes
