"""Authorized, redacted account evidence for high-risk Amazon diagnostics.

This path intentionally accepts text/JSON fields rather than raw identity
documents.  It produces an ordinary reviewed knowledge draft and stores only a
sanitized card plus a metadata ledger after explicit confirmation.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from . import ads_evidence, config, knowledge, locking, security


EVIDENCE_KINDS = {
    "performance_notification": "Account Health or performance notification",
    "seller_central_help_export": "Authorized Seller Central help export",
    "support_case": "Seller Support or Account Health case response",
    "listing_issue": "Listing issue or processing result",
    "registration_notice": "Registration or identity-verification notice",
    "compliance_notice": "Product compliance, restricted-product, dangerous-goods, or IP notice",
    "fee_record": "Fee estimate, preview, or observed transaction record",
    "api_notification": "Authorized SP-API notification or response",
    "advertising_report": "Authorized Amazon Ads report snapshot",
    "traffic_experiment": "Authorized advertising or traffic experiment",
    "tax_report": "Authorized seller tax report or tax document notice",
    "settlement_report": "Authorized finance transaction or settlement record",
    "returns_report": "Authorized return, refund, or SAFE-T claim record",
    "brand_notice": "Authorized Brand Registry or brand-protection notice",
}
MARKETPLACES = {
    "GLOBAL", "US", "CA", "MX", "BR", "UK", "DE", "FR", "IT", "ES", "NL", "SE", "PL",
    "BE", "IE", "JP", "AU", "SG", "IN", "AE", "SA", "EG", "TR",
}
_MAX_CONTENT_CHARS = 500_000
_SOURCE_HOST_PATTERNS = (
    re.compile(r"^sellercentral(?:-europe)?\.amazon\.[a-z.]+$", re.I),
    re.compile(r"^sell\.amazon\.[a-z.]+$", re.I),
    re.compile(r"^developer-docs\.amazon(?:\.com)?$", re.I),
    re.compile(r"^advertising\.amazon\.com$", re.I),
)
_PRIVATE_PATTERNS = [
    ("email", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "***EMAIL_REDACTED***"),
    (
        "phone",
        re.compile(r"(?im)((?:phone|telephone|mobile|手机号|电话)\s*[:：=]\s*)[^\n]{5,40}"),
        r"\1***PHONE_REDACTED***",
    ),
    (
        "address",
        re.compile(r"(?im)((?:residential|registered|business|home|billing)?\s*address|住址|地址)\s*[:：=]\s*[^\n]+"),
        r"\1: ***ADDRESS_REDACTED***",
    ),
    (
        "identity_number",
        re.compile(
            r"(?im)((?:passport|identity|government.?id|national.?id|document|身份证|护照|证件)"
            r"(?:\s*(?:number|no\.?|号码))?\s*[:：=]\s*)[^\n]{3,80}"
        ),
        r"\1***IDENTITY_REDACTED***",
    ),
    (
        "bank_or_card",
        re.compile(
            r"(?im)((?:bank\s*account|account\s*number|credit\s*card|card\s*number|iban|swift|"
            r"银行账号|银行卡号|信用卡号)\s*[:：=]\s*)[^\n]{3,100}"
        ),
        r"\1***FINANCIAL_REDACTED***",
    ),
    (
        "tax_id",
        re.compile(r"(?im)((?:tax\s*id|vat\s*(?:id|number)|ein|税号)\s*[:：=]\s*)[^\n]{3,80}"),
        r"\1***TAX_ID_REDACTED***",
    ),
    (
        "legal_name",
        re.compile(
            r"(?im)((?:full\s+legal\s+name|primary\s+contact|legal\s+representative|法定代表人|"
            r"联系人姓名)\s*[:：=]\s*)[^\n]{2,100}"
        ),
        r"\1***NAME_REDACTED***",
    ),
]


def _ledger_file() -> Path:
    return config.IVYEA_DIR / "knowledge" / "evidence.jsonl"


def _ledger_lock_file() -> Path:
    return config.IVYEA_DIR / "knowledge" / ".evidence.lock"


def schema() -> dict[str, Any]:
    path = resources.files("ivyea_agent").joinpath("knowledge_base/knowledge_evidence_schema.json")
    return json.loads(path.read_text(encoding="utf-8"))


def redact_evidence_text(text: str) -> tuple[str, dict[str, int]]:
    """Apply evidence-specific PII redaction in addition to secret redaction."""
    out = security.redact_text(str(text or ""))
    counts: dict[str, int] = {}
    for name, pattern, replacement in _PRIVATE_PATTERNS:
        out, count = pattern.subn(replacement, out)
        if count:
            counts[name] = count
    return out, counts


def _redact_structure(value: Any) -> tuple[Any, dict[str, int]]:
    """Redact all strings in a diagnostic tree before it can be persisted."""
    counts: dict[str, int] = {}
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            clean_item, item_counts = _redact_structure(item)
            clean[key] = clean_item
            for name, count in item_counts.items():
                counts[name] = counts.get(name, 0) + count
        return clean, counts
    if isinstance(value, list):
        clean_list = []
        for item in value:
            clean_item, item_counts = _redact_structure(item)
            clean_list.append(clean_item)
            for name, count in item_counts.items():
                counts[name] = counts.get(name, 0) + count
        return clean_list, counts
    if isinstance(value, tuple):
        clean_tuple, counts = _redact_structure(list(value))
        return tuple(clean_tuple), counts
    if isinstance(value, str):
        return redact_evidence_text(value)
    return value, counts


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()


def _private_ref(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    suffix = re.sub(r"[^A-Za-z0-9]", "", raw)[-4:]
    return f"sha256:{_hash(raw)[:16]}" + (f":last4={suffix}" if suffix else "")


def _safe_identifier(value: str, *, kind: str, max_len: int = 80) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if kind == "asin":
        return raw.upper() if re.fullmatch(r"[A-Za-z0-9]{10}", raw) else ""
    if kind in {"sku", "product_type", "error_code"}:
        return raw[:max_len] if re.fullmatch(r"[A-Za-z0-9._:/ -]+", raw) else ""
    return raw[:max_len]


def _source_url(payload: dict[str, Any], kind: str) -> str:
    raw = str(payload.get("source_url") or "").strip()
    if not raw:
        return f"sellercentral-export://{kind}"
    parsed = urlparse(raw)
    if parsed.scheme == "sellercentral-export":
        return raw
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(pattern.fullmatch(host) for pattern in _SOURCE_HOST_PATTERNS):
        raise ValueError("source_url must be an official Amazon URL or sellercentral-export:// reference")
    safe_segments = []
    for segment in parsed.path.split("/"):
        if re.search(r"[A-Fa-f0-9]{16,}|\d{10,}", segment) or len(segment) > 100:
            safe_segments.append("REDACTED-REF")
        else:
            safe_segments.append(segment)
    return urlunparse((parsed.scheme, parsed.netloc, "/".join(safe_segments), "", "", ""))


def _diagnostic(payload: dict[str, Any], kind: str, marketplace: str, message: str) -> dict[str, Any]:
    if kind in {"advertising_report", "traffic_experiment"}:
        result = ads_evidence.analyze(payload)
        result.update({
            "kind": kind,
            "marketplace": marketplace,
            "error_code": "",
            "asin": _safe_identifier(payload.get("asin", ""), kind="asin"),
            "sku": _safe_identifier(payload.get("sku", ""), kind="sku"),
            "product_type": "",
            "ready_for_diagnosis": bool(result.get("ready_for_analysis")),
            "required_fields": (
                ["marketplace", "ad_product", "report_type", "currency", "time_zone", "window_start", "window_end", "metrics"]
                if kind == "advertising_report"
                else ["marketplace", "ad_product", "report_type", "currency", "time_zone", "hypothesis", "changed_factors", "baseline", "evaluation"]
            ),
        })
        return result
    error_code = _safe_identifier(payload.get("error_code", ""), kind="error_code")
    asin = _safe_identifier(payload.get("asin", ""), kind="asin")
    sku = _safe_identifier(payload.get("sku", ""), kind="sku")
    product_type = _safe_identifier(payload.get("product_type", ""), kind="product_type")
    account_status = str(payload.get("account_status") or "").upper().strip()
    if account_status not in {"", "NORMAL", "AT_RISK", "DEACTIVATED"}:
        raise ValueError("account_status must be NORMAL, AT_RISK, or DEACTIVATED")

    required = ["marketplace", "exact_message"]
    if kind == "listing_issue":
        required.extend(["error_code", "sku_or_asin", "product_type"])
    elif kind in {"performance_notification", "compliance_notice"}:
        required.extend(["notification_reference", "policy_or_status"])
    elif kind == "fee_record":
        required.extend(["sku_or_asin", "observed_or_estimate", "currency"])
    elif kind == "registration_notice":
        required.extend(["registration_stage", "marketplace_specific_document_request"])
    elif kind == "tax_report":
        required.extend(["report_type", "currency"])
    elif kind == "settlement_report":
        required.extend(["settlement_reference", "currency"])
    elif kind == "returns_report":
        required.extend(["order_or_claim_reference", "sku_or_asin"])
    elif kind == "brand_notice":
        required.extend(["notification_reference", "program_or_policy"])

    missing = []
    if not marketplace:
        missing.append("marketplace")
    if not message:
        missing.append("exact_message")
    if kind == "listing_issue":
        if not error_code:
            missing.append("error_code")
        if not (sku or asin):
            missing.append("sku_or_asin")
        if not product_type:
            missing.append("product_type")
    if kind in {"performance_notification", "compliance_notice"}:
        if not (payload.get("notification_id") or payload.get("case_id")):
            missing.append("notification_reference")
        if not (payload.get("policy") or account_status):
            missing.append("policy_or_status")
    if kind == "fee_record":
        if not (sku or asin):
            missing.append("sku_or_asin")
        if str(payload.get("record_type") or "") not in {"observed", "estimate"}:
            missing.append("observed_or_estimate")
        if not payload.get("currency"):
            missing.append("currency")
    if kind == "registration_notice":
        if not payload.get("registration_stage"):
            missing.append("registration_stage")
        if not payload.get("document_request"):
            missing.append("marketplace_specific_document_request")
    if kind == "tax_report":
        if not payload.get("report_type"):
            missing.append("report_type")
        if not payload.get("currency"):
            missing.append("currency")
    if kind == "settlement_report":
        if not (payload.get("settlement_id") or payload.get("transaction_id")):
            missing.append("settlement_reference")
        if not payload.get("currency"):
            missing.append("currency")
    if kind == "returns_report":
        if not (payload.get("order_id") or payload.get("case_id") or payload.get("claim_id")):
            missing.append("order_or_claim_reference")
        if not (sku or asin):
            missing.append("sku_or_asin")
    if kind == "brand_notice":
        if not (payload.get("notification_id") or payload.get("case_id")):
            missing.append("notification_reference")
        if not (payload.get("program") or payload.get("policy")):
            missing.append("program_or_policy")

    return {
        "kind": kind,
        "marketplace": marketplace,
        "error_code": error_code,
        "exact_message_present": bool(message),
        "asin": asin,
        "sku": sku,
        "product_type": product_type,
        "account_status": account_status,
        "policy": redact_evidence_text(str(payload.get("policy") or "").strip()[:200])[0],
        "record_type": str(payload.get("record_type") or "").strip(),
        "currency": str(payload.get("currency") or "").upper().strip()[:8],
        "report_type": _safe_identifier(payload.get("report_type", ""), kind="product_type"),
        "program": _safe_identifier(payload.get("program", ""), kind="product_type"),
        "required_fields": required,
        "missing_inputs": sorted(set(missing)),
        "ready_for_diagnosis": not bool(missing),
        "reasoning_boundary": (
            "The exact account notice/schema/transaction is account evidence; it does not become a universal Amazon rule."
        ),
    }


def _body(
    *, title: str, kind: str, marketplace: str, locale: str, source_url: str,
    captured_at: str, observed_at: str, message: str, content: str,
    diagnostic: dict[str, Any], refs: dict[str, str], redactions: dict[str, int],
) -> str:
    meta = [
        f"- evidence_kind: {kind}",
        f"- marketplace: {marketplace}",
        f"- locale: {locale}",
        f"- captured_at: {captured_at}",
        f"- observed_at: {observed_at}",
        f"- source_url: {source_url}",
        "- authority: account_authorized_official_context",
    ]
    for name, value in refs.items():
        if value:
            meta.append(f"- {name}: {value}")
    sections = [f"# {title}", "", "## Evidence metadata", "", *meta]
    if message:
        sections.extend(["", "## Exact message", "", message])
    sections.extend([
        "", "## Authorized sanitized content", "", content,
        "", "## Structured diagnostic record", "", "```json",
        json.dumps(diagnostic, ensure_ascii=False, indent=2), "```",
        "", "## Redaction summary", "", json.dumps(redactions, ensure_ascii=False, sort_keys=True),
        "", "## Use boundary", "",
        "This is private account-local evidence. It can support diagnosis for the recorded marketplace/account context, "
        "but must not be generalized into an Amazon-wide rule.",
    ])
    return "\n".join(sections).strip() + "\n"


def prepare(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a reviewed knowledge draft from explicitly authorized account evidence."""
    if not isinstance(payload, dict):
        raise ValueError("evidence payload must be an object")
    if payload.get("authorized") is not True or payload.get("rights_confirmed") is not True:
        raise ValueError("authorized=true and rights_confirmed=true are required")
    kind = str(payload.get("kind") or "").strip()
    if kind not in EVIDENCE_KINDS:
        raise ValueError("unknown evidence kind; available: " + ", ".join(sorted(EVIDENCE_KINDS)))
    marketplace = str(payload.get("marketplace") or "").upper().strip()
    if marketplace not in MARKETPLACES:
        raise ValueError("a supported marketplace is required")
    content_raw = str(payload.get("content") or "").strip()
    message_raw = str(payload.get("exact_message") or "").strip()
    if not content_raw and not message_raw and kind not in {"advertising_report", "traffic_experiment"}:
        raise ValueError("content or exact_message is required")
    if len(content_raw) > _MAX_CONTENT_CHARS:
        raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")

    content, content_redactions = redact_evidence_text(content_raw)
    message, message_redactions = redact_evidence_text(message_raw)
    redactions = dict(content_redactions)
    for name, count in message_redactions.items():
        redactions[name] = redactions.get(name, 0) + count
    source_url = _source_url(payload, kind)
    captured_at = str(payload.get("captured_at") or time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    observed_at = str(payload.get("observed_at") or captured_at)
    locale = str(payload.get("locale") or "en-US").strip()[:20]
    diagnostic, diagnostic_redactions = _redact_structure(_diagnostic(payload, kind, marketplace, message))
    for name, count in diagnostic_redactions.items():
        redactions[name] = redactions.get(name, 0) + count
    refs = {
        "account_ref": _private_ref(payload.get("account_id", "")),
        "case_ref": _private_ref(payload.get("case_id", "")),
        "notification_ref": _private_ref(payload.get("notification_id", "")),
        "profile_ref": _private_ref(payload.get("profile_id", "")),
        "campaign_ref": _private_ref(payload.get("campaign_id", "")),
        "ad_group_ref": _private_ref(payload.get("ad_group_id", "")),
        "order_ref": _private_ref(payload.get("order_id", "")),
        "claim_ref": _private_ref(payload.get("claim_id", "")),
        "settlement_ref": _private_ref(payload.get("settlement_id", "")),
        "transaction_ref": _private_ref(payload.get("transaction_id", "")),
        "asin": diagnostic["asin"],
        "sku": diagnostic["sku"],
        "product_type": diagnostic["product_type"],
        "error_code": diagnostic["error_code"],
    }
    fingerprint_input = "|".join([
        kind, marketplace, source_url, refs["case_ref"], refs["notification_ref"], refs["asin"], refs["sku"],
        refs["profile_ref"], refs["campaign_ref"], refs["ad_group_ref"],
        refs["order_ref"], refs["claim_ref"], refs["settlement_ref"], refs["transaction_ref"],
        diagnostic["error_code"], message, content,
        json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
    ])
    evidence_id = "ev-" + _hash(fingerprint_input)[:20]
    title = redact_evidence_text(str(payload.get("title") or EVIDENCE_KINDS[kind]).strip()[:160])[0]
    body = _body(
        title=title, kind=kind, marketplace=marketplace, locale=locale, source_url=source_url,
        captured_at=captured_at, observed_at=observed_at, message=message, content=content,
        diagnostic=diagnostic, refs=refs, redactions=redactions,
    )
    card_id = str(payload.get("card_id") or f"user.evidence.{kind}.{evidence_id[3:15]}")
    tags = [
        "authorized-evidence", kind, marketplace.lower(),
        *[value for value in (diagnostic["error_code"], diagnostic["product_type"]) if value],
    ]
    if kind in {"advertising_report", "traffic_experiment"}:
        context = diagnostic.get("context") or {}
        tags.extend(value for value in (context.get("ad_product"), context.get("report_type")) if value)
    draft = knowledge.draft_update(
        title,
        body,
        source_url=source_url,
        source_type="account_authorized_official_evidence",
        confidence="account_observed",
        tags=tags,
        card_id=card_id,
        license="user_authorized_private_evidence",
    )
    draft["review_required"] = True
    draft["warnings"] = list(draft.get("warnings") or []) + [
        "private_account_evidence_review_required",
        *(["diagnostic_inputs_incomplete"] if diagnostic["missing_inputs"] else []),
    ]
    record = {
        "id": evidence_id,
        "card_id": draft["card_id"],
        "title": title,
        "kind": kind,
        "marketplace": marketplace,
        "locale": locale,
        "source_url": source_url,
        "captured_at": captured_at,
        "observed_at": observed_at,
        "refs": refs,
        "diagnostic": diagnostic,
        "redactions": redactions,
        "body_hash": draft["new_hash"],
        "authorization": "user_confirmed",
    }
    return {"ok": True, "evidence": record, "draft": draft, "raw_preserved": False}


def _append_record(record: dict[str, Any]) -> None:
    with locking.exclusive_file_lock(_ledger_lock_file()):
        path = _ledger_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [row for row in list_evidence(limit=1000)["evidence"] if row.get("id") != record.get("id")]
        rows.append(record)
        rows.sort(key=lambda row: str(row.get("captured_at") or ""), reverse=True)
        temp = path.with_name(path.name + f".{time.time_ns()}.tmp")
        temp.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        temp.replace(path)


def apply(prepared: dict[str, Any], *, confirm: bool = False, rebuild_indexes: bool = True) -> dict[str, Any]:
    if not isinstance(prepared, dict) or not prepared.get("ok"):
        return {"ok": False, "applied": False, "error": "invalid_evidence_draft"}
    draft = prepared.get("draft") if isinstance(prepared.get("draft"), dict) else {}
    record = prepared.get("evidence") if isinstance(prepared.get("evidence"), dict) else {}
    actual_body_hash = _hash(str(draft.get("body") or "").strip() + "\n")
    if (
        not draft or not record
        or record.get("body_hash") != draft.get("new_hash")
        or actual_body_hash != draft.get("new_hash")
    ):
        return {"ok": False, "applied": False, "error": "evidence_integrity_check_failed"}
    result = knowledge.apply_update(draft, confirm=confirm, rebuild_indexes=False)
    if result.get("ok") and result.get("applied"):
        knowledge.annotate_user_card(str(record.get("card_id") or ""), {
            "marketplaces": [record.get("marketplace")],
            "locales": [record.get("locale")],
            "evidence_id": record.get("id"),
            "evidence_kind": record.get("kind"),
            "observed_at": record.get("observed_at"),
            "captured_at": record.get("captured_at"),
            "diagnostic": record.get("diagnostic") or {},
            "authority_tier": "account_local",
            "evidence_class": "account_authorized_official_evidence",
            "source_quality": "account_observed_official_context",
        })
        if rebuild_indexes:
            indexes: dict[str, Any] = {"knowledge": knowledge.rebuild_index()}
            try:
                from . import retrieval
                indexes["retrieval"] = retrieval.rebuild_index()
            except Exception as exc:  # noqa: BLE001 - evidence remains safely stored; index error is reported
                indexes["retrieval_error"] = security.redact_text(str(exc))
            result["indexes"] = indexes
        stored = {
            **record,
            "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": "active",
        }
        _append_record(stored)
    return {"ok": bool(result.get("ok")), "evidence": record, "draft": draft, "result": result, "raw_preserved": False}


def list_evidence(limit: int = 100) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    try:
        lines = _ledger_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    rows = rows[:max(1, min(int(limit or 100), 1000))]
    return {
        "summary": {
            "evidence": len(rows),
            "ready_for_diagnosis": sum(1 for row in rows if (row.get("diagnostic") or {}).get("ready_for_diagnosis")),
            "raw_documents_preserved": 0,
        },
        "evidence": rows,
        "ledger": str(_ledger_file()),
    }


def render_prepared(prepared: dict[str, Any]) -> str:
    evidence = prepared.get("evidence") or {}
    diagnostic = evidence.get("diagnostic") or {}
    lines = [
        "Ivyea 授权证据导入草案：",
        f"- id={evidence.get('id')} kind={evidence.get('kind')} marketplace={evidence.get('marketplace')}",
        f"- card={evidence.get('card_id')} ready_for_diagnosis={diagnostic.get('ready_for_diagnosis')}",
        f"- missing_inputs={','.join(diagnostic.get('missing_inputs') or []) or '-'}",
        f"- redactions={json.dumps(evidence.get('redactions') or {}, ensure_ascii=False, sort_keys=True)}",
        "- raw_preserved=false; 只保存脱敏文本，确认前不写入知识库。",
        "",
        knowledge.render_update_draft(prepared.get("draft") or {}),
    ]
    return "\n".join(lines)


def render_list(limit: int = 100) -> str:
    data = list_evidence(limit=limit)
    if not data["evidence"]:
        return "（暂无已应用的授权账户证据）"
    lines = [
        "Ivyea 授权账户证据：",
        f"- evidence={data['summary']['evidence']} ready={data['summary']['ready_for_diagnosis']} raw_documents=0",
    ]
    for row in data["evidence"]:
        diag = row.get("diagnostic") or {}
        lines.append(
            f"- {row.get('id')} | {row.get('kind')} | {row.get('marketplace')} | "
            f"ready={diag.get('ready_for_diagnosis')} | card={row.get('card_id')}"
        )
    return "\n".join(lines)


def render_apply(result: dict[str, Any]) -> str:
    applied = result.get("result") or {}
    if not applied.get("ok"):
        return f"授权证据未应用：{applied.get('error', result.get('error', 'unknown'))}"
    if not applied.get("applied"):
        return f"授权证据无需应用：action={applied.get('action', 'noop')}"
    evidence = result.get("evidence") or {}
    return (
        f"已应用授权证据：{evidence.get('id')} -> {evidence.get('card_id')}\n"
        "raw_preserved=false；证据正文已专项脱敏，结构化元数据已写入审计台账。"
    )
