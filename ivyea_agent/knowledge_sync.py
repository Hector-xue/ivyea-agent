"""Monitor approved Amazon sources without publishing unreviewed content.

The monitor deliberately stops at a review queue.  A changed web page is not a
knowledge card until an operator reviews and applies it through the existing
knowledge update flow.
"""
from __future__ import annotations

import difflib
import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from . import config, locking, security


OFFICIAL_HOSTS = {
    "advertising.amazon.com",
    "developer-docs.amazon",
    "developer-docs.amazon.com",
    "sell.amazon.ae",
    "sell.amazon.ca",
    "sell.amazon.com.au",
    "sell.amazon.com.sg",
    "sell.amazon.de",
    "sell.amazon.in",
    "sell.amazon.com",
    "sell.amazon.co.jp",
    "sell.amazon.co.uk",
    "sellercentral.amazon.com",
    "sellercentral.amazon.co.jp",
    "sellercentral.amazon.co.uk",
    "sellercentral.amazon.de",
}
FETCHABLE_MODES = {"html", "html_monitor", "llms_txt", "markdown", "rss"}
REVIEW_DECISIONS = {"approved", "rejected", "superseded"}
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_SNAPSHOT_CHARS = 2_000_000


def _registry_file():
    return resources.files("ivyea_agent").joinpath("knowledge_base/knowledge_sources.json")


def _monitor_dir() -> Path:
    return config.IVYEA_DIR / "knowledge" / "monitor"


def _state_file() -> Path:
    return _monitor_dir() / "state.json"


def _events_file() -> Path:
    return _monitor_dir() / "events.jsonl"


def _reviews_file() -> Path:
    return _monitor_dir() / "reviews.jsonl"


def _publications_file() -> Path:
    return _monitor_dir() / "publications.jsonl"


def _sync_lock_file() -> Path:
    return _monitor_dir() / ".sync.lock"


def _ledger_lock_file() -> Path:
    return _monitor_dir() / ".ledger.lock"


def _publication_lock_file() -> Path:
    return _monitor_dir() / ".publication.lock"


def load_registry() -> list[dict[str, Any]]:
    """Load and validate the bundled allowlist of official sources."""
    rows = json.loads(_registry_file().read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("knowledge source registry must be a list")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("knowledge source entry must be an object")
        row = dict(raw)
        source_id = str(row.get("id") or "").strip()
        url = str(row.get("url") or "").strip()
        parsed = urlparse(url)
        if not source_id or source_id in seen:
            raise ValueError(f"invalid or duplicate knowledge source id: {source_id!r}")
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in OFFICIAL_HOSTS:
            raise ValueError(f"knowledge source is outside the official allowlist: {url}")
        for key in ("authority_tier", "evidence_class", "update_mode", "sync_policy"):
            if not str(row.get(key) or "").strip():
                raise ValueError(f"knowledge source {source_id} missing {key}")
        row["enabled"] = bool(row.get("enabled", True))
        row["requires_auth"] = bool(row.get("requires_auth", False))
        row["cadence_hours"] = max(1.0, float(row.get("cadence_hours") or 24))
        row["topics"] = [str(v) for v in row.get("topics") or []]
        row["marketplaces"] = [str(v) for v in row.get("marketplaces") or ["GLOBAL"]]
        row["locales"] = [str(v) for v in row.get("locales") or ["en-US"]]
        seen.add(source_id)
        out.append(row)
    return out


def registry() -> dict[str, Any]:
    rows = load_registry()
    return {
        "summary": {
            "sources": len(rows),
            "enabled": sum(1 for row in rows if row["enabled"]),
            "public_monitorable": sum(
                1 for row in rows
                if row["enabled"] and not row["requires_auth"] and row["update_mode"] in FETCHABLE_MODES
            ),
            "authorization_required": sum(1 for row in rows if row["requires_auth"]),
            "primary_sources": sum(1 for row in rows if str(row["authority_tier"]).startswith("primary")),
        },
        "sources": rows,
    }


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": 1, "sources": {}}
    if not isinstance(data, dict):
        return {"version": 1, "sources": {}}
    data.setdefault("version", 1)
    data.setdefault("sources", {})
    return data


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{time.time_ns()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{time.time_ns()}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _append_event(event: dict[str, Any]) -> None:
    path = _events_file()
    with locking.exclusive_file_lock(_ledger_lock_file()):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()


def _append_review(review: dict[str, Any]) -> None:
    path = _reviews_file()
    with locking.exclusive_file_lock(_ledger_lock_file()):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(review, ensure_ascii=False) + "\n")
            fh.flush()


def _append_publication(publication: dict[str, Any]) -> None:
    path = _publications_file()
    with locking.exclusive_file_lock(_ledger_lock_file()):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(publication, ensure_ascii=False) + "\n")
            fh.flush()


def _event_id(event: dict[str, Any]) -> str:
    existing = str(event.get("event_id") or "").strip()
    if existing:
        return existing
    material = "|".join([
        str(event.get("id") or ""),
        str(event.get("content_hash") or ""),
        str(event.get("checked_at") or ""),
    ])
    return "chg-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _redact_review_text(value: str) -> str:
    text = security.redact_text(str(value or ""))
    text = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "***EMAIL_REDACTED***", text)
    text = re.sub(
        r"(?im)((?:phone|telephone|mobile|手机号|电话)\s*[:：=]\s*)[^\n]{5,40}",
        r"\1***PHONE_REDACTED***",
        text,
    )
    return text


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|svg|noscript|template)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<(?:br|/p|/div|/li|/h[1-6]|/tr|/section|/article)>\s*", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _rss_text(text: str) -> str:
    root = ET.fromstring(text)
    rows: list[str] = []
    for item in root.findall(".//item"):
        values = []
        for tag in ("title", "link", "pubDate", "description"):
            value = item.findtext(tag) or ""
            if tag == "description":
                value = _clean_html(value)
            value = re.sub(r"\s+", " ", html.unescape(value)).strip()
            if value:
                values.append(f"{tag}: {value}")
        if values:
            rows.append(" | ".join(values))
    if rows:
        return "\n".join(rows)
    return _clean_html(text)


def normalize_content(text: str, mode: str) -> str:
    if mode == "rss":
        normalized = _rss_text(text)
    elif mode in {"html", "html_monitor"}:
        normalized = _clean_html(text)
    else:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = "\n".join(line.rstrip() for line in normalized.splitlines())
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return security.redact_text(normalized[:_MAX_SNAPSHOT_CHARS])


def _default_fetcher(source: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    import httpx

    headers = {
        "User-Agent": "IvyeaAgent-KnowledgeMonitor/1.0 (+review-only)",
        "Accept": "application/rss+xml, application/xml, text/markdown, text/plain, text/html;q=0.9, */*;q=0.1",
    }
    if previous.get("etag"):
        headers["If-None-Match"] = str(previous["etag"])
    if previous.get("last_modified"):
        headers["If-Modified-Since"] = str(previous["last_modified"])
    response = httpx.get(str(source["url"]), headers=headers, timeout=20.0, follow_redirects=True)
    if response.status_code == 304:
        return {"status": 304, "text": "", "headers": dict(response.headers), "url": str(response.url)}
    response.raise_for_status()
    if len(response.content) > _MAX_BODY_BYTES:
        raise ValueError(f"source response exceeds {_MAX_BODY_BYTES} bytes")
    final = urlparse(str(response.url))
    if final.scheme != "https" or (final.hostname or "").lower() not in OFFICIAL_HOSTS:
        raise ValueError(f"source redirected outside official allowlist: {response.url}")
    return {"status": response.status_code, "text": response.text, "headers": dict(response.headers), "url": str(response.url)}


def _diff(old: str, new: str, limit: int = 12000) -> str:
    value = "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile="previous", tofile="current", lineterm="",
    ))
    return value[:limit]


def _snapshot_path(source_id: str, epoch: float, digest: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", source_id).strip(".-") or "source"
    stamp = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _monitor_dir() / "snapshots" / safe_id / f"{stamp}-{digest[:12]}.txt"


@locking.serialized(_sync_lock_file, timeout=30.0)
def sync(
    *,
    force: bool = False,
    source_ids: list[str] | None = None,
    fetcher: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Check due public sources and enqueue changes for review."""
    epoch = float(now if now is not None else time.time())
    selected = set(source_ids or [])
    sources = load_registry()
    known = {row["id"] for row in sources}
    unknown = sorted(selected - known)
    if unknown:
        raise ValueError("unknown knowledge source: " + ", ".join(unknown))
    state = _load_state()
    state_rows = state["sources"]
    fetch = fetcher or _default_fetcher
    results: list[dict[str, Any]] = []

    for source in sources:
        source_id = source["id"]
        if selected and source_id not in selected:
            continue
        previous = dict(state_rows.get(source_id) or {})
        base = {"id": source_id, "title": source["title"], "url": source["url"]}
        if not source["enabled"]:
            results.append({**base, "status": "disabled"})
            continue
        if source["requires_auth"] or source["update_mode"] not in FETCHABLE_MODES:
            results.append({
                **base,
                "status": "authorization_required",
                "reason": source["sync_policy"],
            })
            continue
        last_checked = float(previous.get("last_checked_epoch") or 0)
        due = force or epoch - last_checked >= float(source["cadence_hours"]) * 3600
        if not due:
            results.append({**base, "status": "not_due", "last_checked": previous.get("last_checked")})
            continue

        checked_at = _stamp(epoch)
        try:
            response = fetch(source, previous)
            status_code = int(response.get("status") or 200)
            headers = {str(k).lower(): str(v) for k, v in (response.get("headers") or {}).items()}
            if status_code == 304:
                previous.update({"last_checked": checked_at, "last_checked_epoch": epoch, "last_status": "unchanged"})
                state_rows[source_id] = previous
                results.append({**base, "status": "unchanged", "checked_at": checked_at, "http_status": 304})
                continue
            normalized = normalize_content(str(response.get("text") or ""), source["update_mode"])
            if not normalized:
                raise ValueError("source returned no usable text")
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            old_text = ""
            old_snapshot = str(previous.get("snapshot") or "")
            if old_snapshot:
                try:
                    old_text = Path(old_snapshot).read_text(encoding="utf-8")
                except OSError:
                    old_text = ""
            first_seen = not bool(previous.get("content_hash"))
            changed = not first_seen and previous.get("content_hash") != digest
            event_status = "new" if first_seen else ("changed" if changed else "unchanged")
            snapshot = old_snapshot
            if first_seen or changed or not old_snapshot:
                path = _snapshot_path(source_id, epoch, digest)
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_text_atomic(path, normalized)
                snapshot = str(path)
            new_state = {
                **previous,
                "last_checked": checked_at,
                "last_checked_epoch": epoch,
                "last_status": event_status,
                "content_hash": digest,
                "snapshot": snapshot,
                "etag": headers.get("etag", previous.get("etag", "")),
                "last_modified": headers.get("last-modified", previous.get("last_modified", "")),
                "final_url": str(response.get("url") or source["url"]),
                "last_error": "",
            }
            if first_seen or changed:
                new_state["last_change"] = checked_at
            state_rows[source_id] = new_state
            result = {
                **base,
                "status": event_status,
                "checked_at": checked_at,
                "content_hash": digest,
                "snapshot": snapshot,
                "review_required": changed,
                "sync_policy": source["sync_policy"],
            }
            if changed:
                result["diff"] = _diff(old_text, normalized)
                event = {
                    **result,
                    "authority_tier": source["authority_tier"],
                    "evidence_class": source["evidence_class"],
                    "category": source.get("category", ""),
                    "topics": source["topics"],
                    "marketplaces": source["marketplaces"],
                    "locales": source["locales"],
                }
                event["event_id"] = _event_id(event)
                result["event_id"] = event["event_id"]
                _append_event(event)
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - one source must not abort the batch
            error = security.redact_text(str(exc))[:500]
            previous.update({
                "last_checked": checked_at,
                "last_checked_epoch": epoch,
                "last_status": "error",
                "last_error": error,
            })
            state_rows[source_id] = previous
            results.append({**base, "status": "error", "checked_at": checked_at, "error": error})

    state["updated_at"] = _stamp(epoch)
    _write_json_atomic(_state_file(), state)
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "ok": counts.get("error", 0) == 0,
        "checked_at": _stamp(epoch),
        "summary": {"selected": len(results), **counts},
        "results": results,
        "review_queue": str(_events_file()),
        "publication": "review_required_before_import",
    }


def status() -> dict[str, Any]:
    state = _load_state()
    queue = changes(limit=1)
    return {
        "registry": registry()["summary"],
        "state": state,
        "reviews": queue["summary"],
        "review_queue": str(_events_file()),
        "review_history": str(_reviews_file()),
    }


def review_history(limit: int = 100, event_id: str = "") -> dict[str, Any]:
    rows = _jsonl_rows(_reviews_file())
    if event_id:
        rows = [row for row in rows if row.get("event_id") == event_id]
    rows = rows[-max(1, min(int(limit or 100), 1000)):]
    rows.reverse()
    return {"summary": {"reviews": len(rows)}, "reviews": rows, "ledger": str(_reviews_file())}


def publication_history(limit: int = 100, event_id: str = "") -> dict[str, Any]:
    rows = _jsonl_rows(_publications_file())
    if event_id:
        rows = [row for row in rows if row.get("event_id") == event_id]
    rows = rows[-max(1, min(int(limit or 100), 1000)):]
    rows.reverse()
    return {"summary": {"publications": len(rows)}, "publications": rows, "ledger": str(_publications_file())}


def changes(limit: int = 50, review_status: str = "") -> dict[str, Any]:
    events = _jsonl_rows(_events_file())
    reviews = _jsonl_rows(_reviews_file())
    publications = _jsonl_rows(_publications_file())
    latest = {}
    for review in reviews:
        latest[str(review.get("event_id") or "")] = review
    latest_publication = {}
    for publication in publications:
        latest_publication[str(publication.get("event_id") or "")] = publication
    rows = []
    for raw in events:
        row = dict(raw)
        row["event_id"] = _event_id(row)
        review = latest.get(row["event_id"])
        publication = latest_publication.get(row["event_id"])
        row["review_status"] = str((review or {}).get("decision") or "pending")
        row["reviewed_at"] = str((review or {}).get("reviewed_at") or "")
        row["reviewer"] = str((review or {}).get("reviewer") or "")
        row["reviewer_source"] = str((review or {}).get("reviewer_source") or "")
        row["review_identity_verified"] = bool((review or {}).get("identity_verified"))
        row["review_note"] = str((review or {}).get("note") or "")
        row["published"] = bool(publication)
        row["published_at"] = str((publication or {}).get("published_at") or "")
        row["published_card_id"] = str((publication or {}).get("card_id") or "")
        row["ready_for_import_draft"] = (
            row["review_status"] == "approved"
            and row["review_identity_verified"]
            and not publication
        )
        rows.append(row)
    counts = {name: sum(1 for row in rows if row["review_status"] == name) for name in ("pending", *sorted(REVIEW_DECISIONS))}
    approved_not_published = sum(
        1 for row in rows if row["review_status"] == "approved" and not row.get("published")
    )
    published = sum(1 for row in rows if row.get("published"))
    unverified_approved = sum(
        1 for row in rows
        if row["review_status"] == "approved" and not row.get("review_identity_verified")
    )
    if review_status:
        allowed = {"pending", *REVIEW_DECISIONS}
        if review_status not in allowed:
            raise ValueError("review_status must be one of: " + ", ".join(sorted(allowed)))
        rows = [row for row in rows if row["review_status"] == review_status]
    rows = rows[-max(1, min(int(limit or 50), 500)):]
    rows.reverse()
    return {
        "summary": {
            "changes": len(events), **counts,
            "approved_not_published": approved_not_published,
            "published": published,
            "unverified_approved": unverified_approved,
        },
        "changes": rows,
        "review_required": counts["pending"] > 0,
        "publication": "approval_only_enables_import_draft; knowledge_publish_requires_separate_confirmed_apply",
    }


def _change_row(event_id: str) -> dict[str, Any]:
    event_id = str(event_id or "").strip()
    raw = next((row for row in reversed(_jsonl_rows(_events_file())) if _event_id(row) == event_id), None)
    if not raw:
        raise ValueError(f"unknown change event: {event_id}")
    event = dict(raw)
    event["event_id"] = event_id
    review = next(
        (row for row in reversed(_jsonl_rows(_reviews_file())) if row.get("event_id") == event_id), None,
    )
    publication = next(
        (row for row in reversed(_jsonl_rows(_publications_file())) if row.get("event_id") == event_id), None,
    )
    event["review_status"] = str((review or {}).get("decision") or "pending")
    event["reviewed_at"] = str((review or {}).get("reviewed_at") or "")
    event["reviewer"] = str((review or {}).get("reviewer") or "")
    event["reviewer_source"] = str((review or {}).get("reviewer_source") or "")
    event["review_identity_verified"] = bool((review or {}).get("identity_verified"))
    event["review_note"] = str((review or {}).get("note") or "")
    event["published"] = bool(publication)
    event["published_at"] = str((publication or {}).get("published_at") or "")
    event["published_card_id"] = str((publication or {}).get("card_id") or "")
    return event


def _verified_snapshot(event: dict[str, Any]) -> str:
    snapshot = Path(str(event.get("snapshot") or ""))
    try:
        text = snapshot.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("change snapshot is missing; draft cannot be trusted") from exc
    actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if actual_hash != str(event.get("content_hash") or ""):
        raise ValueError("change snapshot integrity check failed")
    return text


def change_packet(event_id: str, card_id: str = "") -> dict[str, Any]:
    """Build a reviewed source packet without turning a web snapshot into knowledge."""
    from . import knowledge

    event = _change_row(event_id)
    if event.get("review_status") != "approved":
        raise ValueError("change must be approved before a knowledge draft can be prepared")
    if not event.get("review_identity_verified"):
        raise ValueError("verified reviewer identity is required before a knowledge draft can be prepared")
    snapshot_text = _verified_snapshot(event)
    topics = {str(value).lower() for value in event.get("topics") or []}
    source_url = str(event.get("url") or "")
    candidates = []
    for card in knowledge.list_cards():
        score = 0
        if source_url and str(card.get("source_url") or "") == source_url:
            score += 100
        overlap = topics.intersection({str(value).lower() for value in card.get("tags") or []})
        score += len(overlap) * 5
        if str(card.get("category") or "") == str(event.get("category") or ""):
            score += 20
        if score:
            candidates.append({
                "id": card.get("id"),
                "title": card.get("title"),
                "category": card.get("category"),
                "source_url": card.get("source_url"),
                "source_type": card.get("source_type"),
                "tags": list(card.get("tags") or []),
                "score": score,
                "exact_source": bool(source_url and str(card.get("source_url") or "") == source_url),
            })
    if not candidates:
        candidates = [
            {
                "id": card.get("id"), "title": card.get("title"), "category": card.get("category"),
                "source_url": card.get("source_url"), "source_type": card.get("source_type"),
                "tags": list(card.get("tags") or []), "score": 0, "exact_source": False,
            }
            for card in knowledge.list_builtin_cards()
            if str(card.get("source_type") or "").startswith(("official", "internal"))
        ]
    candidates.sort(key=lambda row: (-int(row["score"]), str(row["id"])))
    candidates = candidates[:20]
    selected_id = str(card_id or "").strip()
    if not selected_id:
        exact = [row for row in candidates if row["exact_source"]]
        if len(exact) == 1:
            selected_id = str(exact[0]["id"])
    target = knowledge.get_card(selected_id) if selected_id else None
    if selected_id and not target:
        raise ValueError(f"unknown target knowledge card: {selected_id}")
    return {
        "event": {
            key: event.get(key) for key in (
                "event_id", "id", "title", "url", "checked_at", "content_hash", "snapshot",
                "diff", "authority_tier", "evidence_class", "category", "topics", "marketplaces", "locales",
                "review_status", "reviewed_at", "reviewer", "review_note", "published",
                "reviewer_source", "review_identity_verified",
                "published_at", "published_card_id",
            )
        },
        "snapshot_excerpt": snapshot_text[:100_000],
        "snapshot_chars": len(snapshot_text),
        "snapshot_truncated": len(snapshot_text) > 100_000,
        "candidates": candidates,
        "target": ({
            "id": target.get("id"),
            "title": target.get("title"),
            "body": target.get("body"),
            "source_url": target.get("source_url"),
            "source_type": target.get("source_type"),
            "license": target.get("license"),
            "tags": list(target.get("tags") or []),
        } if target else None),
        "selection_required": target is None,
        "publication_boundary": (
            "This packet is source evidence, not publishable knowledge. An operator must edit a concise body, "
            "preview the diff, and separately confirm apply."
        ),
    }


def prepare_change_draft(
    event_id: str,
    *,
    card_id: str = "",
    body: str = "",
    title: str = "",
    new_card_id: str = "",
) -> dict[str, Any]:
    from . import knowledge

    packet = change_packet(event_id, card_id=card_id)
    if not str(body or "").strip():
        return {"ok": True, "draft_ready": False, "packet": packet}
    target = packet.get("target") or {}
    source_id = str((packet.get("event") or {}).get("id") or "official-source")
    digest = str((packet.get("event") or {}).get("content_hash") or "")
    safe_source = re.sub(r"[^a-zA-Z0-9]+", "-", source_id).strip("-").lower() or "source"
    runtime_card_id = str(new_card_id or f"user.official_update.{safe_source}.{digest[:12]}")
    if not runtime_card_id.startswith("user."):
        raise ValueError("reviewed runtime update card_id must start with user.")
    if any(card.get("id") == runtime_card_id for card in knowledge.list_builtin_cards()):
        raise ValueError("reviewed runtime update must not replace a bundled card id")
    provenance = (
        f"<!-- ivyea-change-event: {event_id}; source-hash: {digest}; "
        f"review-target: {target.get('id') or '-'} -->\n"
    )
    proposed_body = provenance + str(body).strip() + "\n"
    draft = knowledge.draft_update(
        str(title or target.get("title") or (packet.get("event") or {}).get("title") or source_id),
        proposed_body,
        source_url=str((packet.get("event") or {}).get("url") or target.get("source_url") or ""),
        source_type="official",
        confidence="high",
        tags=list(target.get("tags") or (packet.get("event") or {}).get("topics") or []),
        card_id=runtime_card_id,
        license=str(target.get("license") or "amazon_public_docs_summary"),
    )
    draft["review_required"] = True
    draft["warnings"] = list(draft.get("warnings") or []) + [
        "official_change_final_confirmation_required",
        "runtime_reviewed_update_does_not_modify_bundled_card",
    ]
    draft["change_event"] = {
        "event_id": event_id,
        "content_hash": digest,
        "target_card_id": target.get("id") or "",
    }
    draft["reviewer"] = str((packet.get("event") or {}).get("reviewer") or "")
    draft["reviewer_source"] = str((packet.get("event") or {}).get("reviewer_source") or "")
    draft["review_identity_verified"] = bool(
        (packet.get("event") or {}).get("review_identity_verified")
    )
    return {"ok": True, "draft_ready": True, "packet": packet, "draft": draft}


@locking.serialized(_publication_lock_file, timeout=30.0)
def apply_change_draft(
    event_id: str,
    *,
    card_id: str = "",
    body: str = "",
    title: str = "",
    new_card_id: str = "",
    confirm: bool = False,
    rebuild_indexes: bool = True,
) -> dict[str, Any]:
    from . import knowledge

    prepared = prepare_change_draft(
        event_id, card_id=card_id, body=body, title=title, new_card_id=new_card_id,
    )
    if not prepared.get("draft_ready"):
        return {**prepared, "ok": False, "applied": False, "error": "draft_body_required"}
    if (prepared.get("packet") or {}).get("event", {}).get("published"):
        return {**prepared, "ok": False, "applied": False, "error": "change_already_published"}
    result = knowledge.apply_update(
        prepared["draft"], confirm=confirm, rebuild_indexes=rebuild_indexes,
    )
    if result.get("ok") and result.get("applied"):
        epoch = time.time()
        publication = {
            "publication_id": "pub-" + hashlib.sha256(
                f"{event_id}|{prepared['draft']['new_hash']}|{epoch}".encode("utf-8")
            ).hexdigest()[:20],
            "event_id": event_id,
            "content_hash": prepared["draft"]["change_event"]["content_hash"],
            "card_id": (result.get("card") or {}).get("id"),
            "body_hash": prepared["draft"]["new_hash"],
            "published_at": _stamp(epoch),
            "publication": "confirmed_runtime_reviewed_update",
        }
        _append_publication(publication)
        result["publication"] = publication
    return {
        "ok": bool(result.get("ok")),
        "applied": bool(result.get("applied")),
        "packet": prepared["packet"],
        "draft": prepared["draft"],
        "result": result,
    }


def review_change(
    event_id: str,
    decision: str,
    *,
    reviewer: str = "local-operator",
    reviewer_source: str = "local_api",
    identity_verified: bool = False,
    note: str = "",
    confirm: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    event_id = str(event_id or "").strip()
    decision = str(decision or "").lower().strip()
    if decision not in REVIEW_DECISIONS:
        raise ValueError("decision must be one of: " + ", ".join(sorted(REVIEW_DECISIONS)))
    if not confirm:
        return {"ok": False, "reviewed": False, "error": "confirmation_required", "event_id": event_id}
    events = changes(limit=500)["changes"]
    event = next((row for row in events if row.get("event_id") == event_id), None)
    if not event:
        raise ValueError(f"unknown change event: {event_id}")
    snapshot = Path(str(event.get("snapshot") or ""))
    try:
        snapshot_text = snapshot.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("change snapshot is missing; review cannot be trusted") from exc
    actual_hash = hashlib.sha256(snapshot_text.encode("utf-8")).hexdigest()
    if actual_hash != str(event.get("content_hash") or ""):
        raise ValueError("change snapshot integrity check failed")
    epoch = float(now if now is not None else time.time())
    clean_reviewer = _redact_review_text(str(reviewer or "local-operator").strip())[:80]
    clean_reviewer_source = re.sub(
        r"[^a-zA-Z0-9_.:-]+", "-", str(reviewer_source or "local_api").strip(),
    )[:80]
    clean_note = _redact_review_text(str(note or "").strip())[:1000]
    prior_status = str(event.get("review_status") or "pending")
    material = (
        f"{event_id}|{decision}|{epoch}|{clean_reviewer}|{clean_reviewer_source}|"
        f"{bool(identity_verified)}|{clean_note}"
    )
    review = {
        "review_id": "rev-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20],
        "event_id": event_id,
        "source_id": event.get("id"),
        "content_hash": event.get("content_hash"),
        "snapshot": str(snapshot),
        "decision": decision,
        "previous_status": prior_status,
        "reviewed_at": _stamp(epoch),
        "reviewer": clean_reviewer,
        "reviewer_source": clean_reviewer_source,
        "identity_verified": bool(identity_verified),
        "identity_trust": "verified" if identity_verified else "asserted",
        "note": clean_note,
        "publication": "not_published",
    }
    _append_review(review)
    return {
        "ok": True,
        "reviewed": True,
        "review": review,
        "ready_for_import_draft": decision == "approved" and bool(identity_verified),
        "knowledge_published": False,
    }


def render_registry() -> str:
    data = registry()
    summary = data["summary"]
    lines = [
        "Ivyea 亚马逊官方来源注册表：",
        f"- sources={summary['sources']} public_monitorable={summary['public_monitorable']} "
        f"authorization_required={summary['authorization_required']} primary={summary['primary_sources']}",
    ]
    for row in data["sources"]:
        auth = "authorized-import" if row["requires_auth"] else "public-monitor"
        lines.append(
            f"- {row['id']} | {row['authority_tier']} | {row['evidence_class']} | {auth} | "
            f"every={row['cadence_hours']:g}h | {row['url']}"
        )
    return "\n".join(lines)


def render_sync(data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    lines = [
        "Ivyea 官方知识源同步：",
        "- " + " ".join(f"{key}={value}" for key, value in summary.items()),
        "- 变更仅进入审核队列，不会自动发布为知识卡。",
    ]
    for row in data.get("results") or []:
        detail = row.get("error") or row.get("reason") or ""
        lines.append(f"- {row['id']} | {row['status']}" + (f" | {detail}" if detail else ""))
    return "\n".join(lines)


def render_changes(limit: int = 50, review_status: str = "") -> str:
    data = changes(limit=limit, review_status=review_status)
    if not data["changes"]:
        return "（暂无待审核的官方来源变更）"
    summary = data["summary"]
    lines = [
        "Ivyea 官方来源变更审核队列：",
        f"- total={summary['changes']} pending={summary['pending']} approved={summary['approved']} "
        f"rejected={summary['rejected']} superseded={summary['superseded']} "
        f"approved_not_published={summary.get('approved_not_published', 0)} published={summary.get('published', 0)}",
    ]
    for row in data["changes"]:
        lines.append(
            f"- {row.get('event_id')} | {row.get('review_status')} | {row.get('checked_at', '-')} | "
            f"{row.get('id')} | {row.get('evidence_class')} | "
            f"{row.get('url')}\n  hash={str(row.get('content_hash') or '')[:12]} snapshot={row.get('snapshot', '-')}"
        )
    return "\n".join(lines)


def render_review(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"官方来源变更未审核：{result.get('error', 'unknown')}"
    review = result["review"]
    return (
        f"官方来源变更已审核：{review['event_id']} -> {review['decision']}\n"
        "knowledge_published=false；批准仅允许继续生成导入草案，仍需独立确认发布。"
    )


def render_review_history(limit: int = 100, event_id: str = "") -> str:
    data = review_history(limit=limit, event_id=event_id)
    if not data["reviews"]:
        return "（暂无官方来源变更审核记录）"
    lines = [f"Ivyea 官方来源变更审核历史：reviews={data['summary']['reviews']}"]
    for row in data["reviews"]:
        lines.append(
            f"- {row.get('review_id')} | {row.get('event_id')} | {row.get('previous_status')} -> "
            f"{row.get('decision')} | {row.get('reviewed_at')} | reviewer={row.get('reviewer')}"
        )
    return "\n".join(lines)
