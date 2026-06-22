"""Unified local retrieval over Ivyea knowledge and memory.

This module is the stable retrieval boundary for CLI, the local HTTP service,
and future IvyeaOps embedding. Today it uses local lexical search/FTS-backed
stores. Vector embeddings can be added behind the same response shape later.
"""
from __future__ import annotations

import re
from typing import Any

from . import knowledge, memory


DEFAULT_SOURCES = ("knowledge", "memory")


def capabilities() -> dict[str, Any]:
    """Describe retrieval features without requiring external services."""
    mem = memory.stats()
    cards = knowledge.list_cards()
    return {
        "local": True,
        "mode": "local_hybrid_lexical",
        "sources": list(DEFAULT_SOURCES),
        "knowledge_cards": len(cards),
        "user_knowledge_cards": len([c for c in cards if c.get("scope") == "user"]),
        "memory_db": mem.get("db", ""),
        "memory_fts": bool(mem.get("fts")),
        "semantic_vectors": {
            "enabled": False,
            "reason": "embedding/vector backend not configured yet; local FTS/LIKE retrieval is active",
        },
    }


def search(query: str, *, limit: int = 8, sources: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """Search local knowledge and memory through one product-facing contract."""
    q = (query or "").strip()
    lim = max(1, min(int(limit or 8), 50))
    wanted = tuple(sources or DEFAULT_SOURCES)
    hits: list[dict[str, Any]] = []
    if not q:
        return {"query": q, "mode": "local_hybrid_lexical", "hits": [], "capabilities": capabilities()}

    if "knowledge" in wanted:
        hits.extend(_knowledge_hits(q, lim))
    if "memory" in wanted:
        hits.extend(_memory_hits(q, lim))

    hits.sort(key=lambda h: (-float(h.get("score") or 0), h.get("source", ""), h.get("id", "")))
    return {
        "query": q,
        "mode": "local_hybrid_lexical",
        "hits": hits[:lim],
        "capabilities": capabilities(),
    }


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
