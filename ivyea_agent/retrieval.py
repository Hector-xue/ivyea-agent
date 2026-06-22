"""Unified local retrieval over Ivyea knowledge and memory.

This module is the stable retrieval boundary for CLI, the local HTTP service,
and future IvyeaOps embedding. Today it uses local lexical search/FTS-backed
stores. Vector embeddings can be added behind the same response shape later.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from . import knowledge, memory, retrieval_index


DEFAULT_SOURCES = ("knowledge", "memory")
RETRIEVAL_MODE = "local_hybrid_lexical_vector"


def capabilities() -> dict[str, Any]:
    """Describe retrieval features without requiring external services."""
    mem = memory.stats()
    cards = knowledge.list_cards()
    return {
        "local": True,
        "mode": RETRIEVAL_MODE,
        "sources": list(DEFAULT_SOURCES),
        "knowledge_cards": len(cards),
        "user_knowledge_cards": len([c for c in cards if c.get("scope") == "user"]),
        "memory_db": mem.get("db", ""),
        "memory_fts": bool(mem.get("fts")),
        "local_vectors": {
            "enabled": True,
            "backend": "local_sparse_vector",
            "external_dependency": False,
        },
        "index": retrieval_index.status(),
        "semantic_vectors": {
            "enabled": False,
            "reason": "neural embedding backend not configured yet; local FTS, sparse vectors and persisted hash index are active",
        },
    }


def search(query: str, *, limit: int = 8, sources: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """Search local knowledge and memory through one product-facing contract."""
    q = (query or "").strip()
    lim = max(1, min(int(limit or 8), 50))
    wanted = tuple(sources or DEFAULT_SOURCES)
    hits: list[dict[str, Any]] = []
    if not q:
        return {"query": q, "mode": RETRIEVAL_MODE, "hits": [], "capabilities": capabilities()}

    if "knowledge" in wanted:
        hits.extend(_knowledge_hits(q, lim))
        hits.extend(retrieval_index.search(q, max(3, min(lim, 8))))
        hits.extend(_knowledge_vector_hits(q, lim))
    if "memory" in wanted:
        hits.extend(_memory_hits(q, lim))

    hits = _dedupe_hits(hits)
    hits.sort(key=lambda h: (-float(h.get("score") or 0), h.get("source", ""), h.get("id", "")))
    return {
        "query": q,
        "mode": RETRIEVAL_MODE,
        "hits": hits[:lim],
        "capabilities": capabilities(),
    }


def index_status() -> dict[str, Any]:
    """Return persisted local retrieval index status."""
    return retrieval_index.status()


def rebuild_index() -> dict[str, Any]:
    """Rebuild the persisted local retrieval index."""
    return retrieval_index.rebuild()


def _knowledge_hits(query: str, limit: int) -> list[dict[str, Any]]:
    rows = knowledge.search(query, limit=limit)
    return [
        {
            "source": "knowledge",
            "id": row.get("id", ""),
            "title": row.get("title", ""),
            "snippet": row.get("snippet", ""),
            "score": int(row.get("score") or 0),
            "scope": row.get("scope", "builtin"),
            "source_type": row.get("source_type", ""),
            "confidence": row.get("confidence", ""),
            "freshness": row.get("freshness", ""),
            "source_quality": row.get("source_quality", ""),
            "source_url": row.get("source_url", ""),
            "tags": list(row.get("tags") or []),
        }
        for row in rows
    ]


def _knowledge_vector_hits(query: str, limit: int) -> list[dict[str, Any]]:
    qvec = _sparse_vector(query)
    if not qvec:
        return []
    rows = []
    for card in knowledge.list_cards():
        body = knowledge.get_card(card["id"]) or card
        text = " ".join([
            str(card.get("id", "")),
            str(card.get("title", "")),
            " ".join(card.get("tags") or []),
            str(body.get("body") or ""),
        ])
        sim = _cosine(qvec, _sparse_vector(text))
        if sim <= 0:
            continue
        rows.append((sim, card, body))
    rows.sort(key=lambda r: (-r[0], r[1].get("id", "")))
    hits = []
    terms = _query_terms(query)
    for sim, card, body in rows[:limit]:
        score = int(10 + sim * 35)
        hits.append({
            "source": "knowledge",
            "id": card.get("id", ""),
            "title": card.get("title", ""),
            "snippet": knowledge._snippet(str(body.get("body") or ""), terms),  # local package helper
            "score": score,
            "scope": card.get("scope", "builtin"),
            "source_type": card.get("source_type", ""),
            "confidence": card.get("confidence", ""),
            "freshness": card.get("freshness", ""),
            "source_quality": card.get("source_quality", ""),
            "source_url": card.get("source_url", ""),
            "tags": list(card.get("tags") or []),
            "match": "local_sparse_vector",
            "vector_score": round(sim, 4),
        })
    return hits


def _memory_hits(query: str, limit: int) -> list[dict[str, Any]]:
    rows = memory.search(query, limit=limit)
    if not rows:
        rows = _memory_term_search(query, limit)
    hits = []
    for i, row in enumerate(rows, start=1):
        text = str(row.get("text") or "")
        hits.append({
            "source": "memory",
            "id": f"memory:{int(row.get('ts') or 0)}:{i}",
            "title": row.get("asin") or "memory",
            "snippet": text[:500],
            "score": _memory_score(query, text, i),
            "asin": row.get("asin", ""),
            "ts": row.get("ts"),
        })
    return hits


def _memory_score(query: str, text: str, rank: int) -> int:
    low = text.lower()
    terms = [t.lower() for t in re.findall(r"[\w\u4e00-\u9fff+.-]+", query) if len(t) >= 2]
    matched = sum(1 for t in terms if t in low)
    return max(1, 25 + min(30, matched * 10) - min(rank, 10))


def _memory_term_search(query: str, limit: int) -> list[dict[str, Any]]:
    seen = set()
    rows = []
    for term in re.findall(r"[\w\u4e00-\u9fff+.-]+", query):
        if not term or len(term) < 2:
            continue
        for row in memory.search(term, limit=limit):
            key = (row.get("text"), row.get("asin"), row.get("ts"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) >= limit:
                return rows
    return rows


def _dedupe_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for hit in hits:
        key = (str(hit.get("source", "")), str(hit.get("id", "")))
        existing = rows.get(key)
        if not existing or float(hit.get("score") or 0) > float(existing.get("score") or 0):
            if existing and existing.get("match") and not hit.get("match"):
                hit["match"] = existing["match"]
            rows[key] = hit
    return list(rows.values())


def _query_terms(text: str) -> list[str]:
    return re.findall(r"[\w\u4e00-\u9fff+.-]+", text)


def _vector_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9+.-]+|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            if len(raw) <= 2:
                tokens.append(raw)
            else:
                tokens.extend(raw[i:i + 2] for i in range(len(raw) - 1))
        else:
            tokens.append(raw)
    return tokens


def _sparse_vector(text: str) -> Counter[str]:
    return Counter(_vector_tokens(text))


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    if dot <= 0:
        return 0.0
    ln = math.sqrt(sum(v * v for v in left.values()))
    rn = math.sqrt(sum(v * v for v in right.values()))
    return dot / (ln * rn) if ln and rn else 0.0


def render_search(result: dict[str, Any]) -> str:
    hits = result.get("hits") or []
    lines = [
        "Ivyea 本地检索",
        "",
        f"- query: {result.get('query', '')}",
        f"- mode: {result.get('mode', '')}",
        f"- hits: {len(hits)}",
    ]
    if not hits:
        lines.append("\n（无匹配结果）")
        return "\n".join(lines)
    lines.append("")
    for hit in hits:
        title = hit.get("title") or hit.get("id") or "-"
        meta = hit.get("source", "")
        if hit.get("confidence"):
            meta += f" confidence={hit.get('confidence')}"
        lines.append(f"- [{meta}] {hit.get('id')} · {title}")
        if hit.get("snippet"):
            lines.append(f"  {hit['snippet']}")
    return "\n".join(lines)
