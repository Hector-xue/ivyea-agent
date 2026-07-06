from __future__ import annotations

import json

import pytest


def _listing_payload() -> dict:
    return {
        "authorized": True,
        "rights_confirmed": True,
        "kind": "listing_issue",
        "marketplace": "US",
        "locale": "en-US",
        "title": "SKU listing issue",
        "source_url": "https://sellercentral.amazon.com/performance/dashboard?caseId=1234567890123456#private",
        "account_id": "A1PRIVATESELLER",
        "case_id": "CASE-123456789",
        "error_code": "90220",
        "exact_message": "item_package_dimensions is required but not supplied",
        "asin": "B0ABCDEF12",
        "sku": "PRIVATE-SKU-1",
        "product_type": "HOME",
        "content": (
            "Contact email: owner@example.com\n"
            "Phone: +1 415 555 1212\n"
            "Residential address: 1 Private Street, Seattle\n"
            "Passport number: P123456789\n"
            "Bank account: 1234567890123456\n"
            "Amazon asks for item_package_dimensions."
        ),
    }


def test_authorized_evidence_requires_explicit_rights_and_official_source(ivyea_home):
    from ivyea_agent import knowledge_evidence

    payload = _listing_payload()
    payload["authorized"] = False
    with pytest.raises(ValueError, match="authorized=true"):
        knowledge_evidence.prepare(payload)

    payload = _listing_payload()
    payload["source_url"] = "https://example.com/copied-help"
    with pytest.raises(ValueError, match="official Amazon URL"):
        knowledge_evidence.prepare(payload)


def test_authorized_evidence_schema_matches_runtime_kinds():
    from ivyea_agent import knowledge_evidence

    schema = knowledge_evidence.schema()
    assert set(schema["properties"]["kind"]["enum"]) == set(knowledge_evidence.EVIDENCE_KINDS)
    assert schema["properties"]["authorized"]["const"] is True
    assert "content" in schema["anyOf"][0]["required"]


def test_prepare_redacts_private_data_and_builds_ready_diagnostic(ivyea_home):
    from ivyea_agent import knowledge, knowledge_evidence

    prepared = knowledge_evidence.prepare(_listing_payload())
    assert prepared["ok"] is True
    assert prepared["raw_preserved"] is False
    assert prepared["evidence"]["diagnostic"]["ready_for_diagnosis"] is True
    assert prepared["evidence"]["diagnostic"]["error_code"] == "90220"
    assert prepared["evidence"]["source_url"] == "https://sellercentral.amazon.com/performance/dashboard"
    assert prepared["evidence"]["refs"]["account_ref"].startswith("sha256:")
    assert "A1PRIVATESELLER" not in json.dumps(prepared, ensure_ascii=False)

    body = prepared["draft"]["body"]
    for private in ("owner@example.com", "415 555 1212", "1 Private Street", "P123456789", "1234567890123456"):
        assert private not in body
    assert "***EMAIL_REDACTED***" in body
    assert "***PHONE_REDACTED***" in body
    assert "***ADDRESS_REDACTED***" in body
    assert "***IDENTITY_REDACTED***" in body
    assert "***FINANCIAL_REDACTED***" in body
    assert knowledge.get_card(prepared["draft"]["card_id"]) is None
    assert knowledge_evidence.list_evidence()["evidence"] == []


def test_incomplete_evidence_is_flagged_not_fabricated(ivyea_home):
    from ivyea_agent import knowledge_evidence

    payload = {
        "authorized": True,
        "rights_confirmed": True,
        "kind": "performance_notification",
        "marketplace": "US",
        "content": "Your account is at risk. Follow the instructions in Account Health.",
    }
    prepared = knowledge_evidence.prepare(payload)
    diagnostic = prepared["evidence"]["diagnostic"]
    assert diagnostic["ready_for_diagnosis"] is False
    assert "notification_reference" in diagnostic["missing_inputs"]
    assert "policy_or_status" in diagnostic["missing_inputs"]
    assert "diagnostic_inputs_incomplete" in prepared["draft"]["warnings"]


@pytest.mark.parametrize(("kind", "extra", "ref_key"), [
    ("tax_report", {"report_type": "SC_VAT_TAX_REPORT", "currency": "EUR"}, ""),
    ("settlement_report", {"settlement_id": "SETTLEMENT-PRIVATE-123", "currency": "USD"}, "settlement_ref"),
    ("returns_report", {"order_id": "ORDER-PRIVATE-123", "sku": "SKU-1"}, "order_ref"),
    ("brand_notice", {"notification_id": "NOTICE-PRIVATE-123", "program": "Transparency"}, "notification_ref"),
])
def test_business_account_evidence_kinds_build_ready_diagnostics(ivyea_home, kind, extra, ref_key):
    from ivyea_agent import knowledge_evidence

    payload = {
        "authorized": True,
        "rights_confirmed": True,
        "kind": kind,
        "marketplace": "US",
        "exact_message": f"Observed {kind} account message",
        "content": "Authorized account-local evidence for diagnosis.",
        **extra,
    }
    prepared = knowledge_evidence.prepare(payload)
    assert prepared["evidence"]["diagnostic"]["ready_for_diagnosis"] is True
    assert prepared["evidence"]["diagnostic"]["missing_inputs"] == []
    if ref_key:
        assert prepared["evidence"]["refs"][ref_key].startswith("sha256:")
        assert next(value for key, value in extra.items() if key.endswith("_id")) not in json.dumps(prepared)


def test_apply_requires_confirmation_then_stores_only_sanitized_evidence(ivyea_home):
    from ivyea_agent import knowledge, knowledge_evidence

    prepared = knowledge_evidence.prepare(_listing_payload())
    card_id = prepared["draft"]["card_id"]
    blocked = knowledge_evidence.apply(prepared, confirm=False)
    assert blocked["ok"] is False
    assert blocked["result"]["error"] == "confirmation_required"
    assert knowledge.get_card(card_id) is None

    applied = knowledge_evidence.apply(prepared, confirm=True, rebuild_indexes=False)
    assert applied["ok"] is True
    card = knowledge.get_card(card_id)
    assert card["source_type"] == "account_authorized_official_evidence"
    assert card["authority_tier"] == "account_local"
    assert card["evidence_class"] == "account_authorized_official_evidence"
    assert card["marketplaces"] == ["US"]
    assert card["diagnostic"]["ready_for_diagnosis"] is True
    assert "owner@example.com" not in card["body"]

    ledger = knowledge_evidence.list_evidence()
    assert ledger["summary"]["evidence"] == 1
    assert ledger["summary"]["raw_documents_preserved"] == 0
    assert ledger["evidence"][0]["card_id"] == card_id


def test_evidence_integrity_check_rejects_tampering(ivyea_home):
    from ivyea_agent import knowledge_evidence

    prepared = knowledge_evidence.prepare(_listing_payload())
    prepared["draft"]["body"] += "tampered"
    result = knowledge_evidence.apply(prepared, confirm=True)
    assert result["ok"] is False
    assert result["error"] == "evidence_integrity_check_failed"


def test_evidence_cli_plan_apply_and_list(ivyea_home, tmp_path, capsys):
    from ivyea_agent.cli import main

    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(_listing_payload(), ensure_ascii=False), encoding="utf-8")

    assert main(["knowledge", "evidence-plan", str(path)]) == 0
    out = capsys.readouterr().out
    assert "授权证据导入草案" in out
    assert "raw_preserved=false" in out
    assert "owner@example.com" not in out

    assert main(["knowledge", "evidence-apply", str(path), "--no-rebuild"]) == 2
    assert "confirmation_required" in capsys.readouterr().out

    assert main(["knowledge", "evidence-apply", str(path), "--confirm", "--no-rebuild"]) == 0
    assert "已应用授权证据" in capsys.readouterr().out
    assert main(["knowledge", "evidence-list"]) == 0
    assert "listing_issue" in capsys.readouterr().out
    assert main(["knowledge", "evidence-schema"]) == 0
    assert "performance_notification" in capsys.readouterr().out
