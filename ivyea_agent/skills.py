"""Reusable Ivyea skills.

A skill is a small, versioned operating playbook that can be loaded into an
agent turn or run from the CLI. Built-in skills live in package data; personal
skills live in ``~/.ivyea/skills``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from . import config, knowledge


@dataclass(frozen=True)
class Skill:
    id: str
    title: str
    domain: str
    version: str
    description: str
    triggers: list[str]
    knowledge_ids: list[str]
    tools: list[str]
    path: str
    scope: str = "builtin"
    body: str = ""


def _builtin_base():
    return resources.files("ivyea_agent").joinpath("skills_builtin")


def _user_base() -> Path:
    return config.IVYEA_DIR / "skills"


def _load_manifest(path: Path | Any, scope: str) -> Skill | None:
    try:
        data = json.loads(path.joinpath("skill.json").read_text(encoding="utf-8"))
        body = path.joinpath("SKILL.md").read_text(encoding="utf-8")
    except Exception:
        return None
    return Skill(
        id=data["id"],
        title=data.get("title", data["id"]),
        domain=data.get("domain", ""),
        version=data.get("version", ""),
        description=data.get("description", ""),
        triggers=list(data.get("triggers") or []),
        knowledge_ids=list(data.get("knowledge_ids") or []),
        tools=list(data.get("tools") or []),
        path=str(path),
        scope=scope,
        body=body,
    )


def _iter_builtin() -> list[Skill]:
    rows: list[Skill] = []
    try:
        base = _builtin_base()
        for domain in base.iterdir():
            if not domain.is_dir():
                continue
            for child in domain.iterdir():
                if child.is_dir():
                    sk = _load_manifest(child, "builtin")
                    if sk:
                        rows.append(sk)
    except Exception:
        pass
    return rows


def _iter_user() -> list[Skill]:
    rows: list[Skill] = []
    base = _user_base()
    if not base.exists():
        return rows
    for child in base.rglob("skill.json"):
        sk = _load_manifest(child.parent, "user")
        if sk:
            rows.append(sk)
    return rows


def list_skills(include_user: bool = True) -> list[Skill]:
    """Return built-in skills, overridden by user skills with the same id."""
    by_id: dict[str, Skill] = {}
    for sk in _iter_builtin():
        by_id[sk.id] = sk
    if include_user:
        for sk in _iter_user():
            by_id[sk.id] = sk
    return sorted(by_id.values(), key=lambda s: (s.domain, s.id))


def get_skill(skill_id: str) -> Skill | None:
    for sk in list_skills():
        if sk.id == skill_id:
            return sk
    return None


def _terms(query: str) -> list[str]:
    return re.findall(r"[\w\u4e00-\u9fff+.-]+", query.lower())


def search(query: str, limit: int = 8) -> list[tuple[Skill, int]]:
    terms = _terms(query)
    if not terms:
        return []
    rows: list[tuple[Skill, int]] = []
    for sk in list_skills():
        hay = " ".join([sk.id, sk.title, sk.description, " ".join(sk.triggers), sk.body]).lower()
        score = sum(hay.count(t) for t in terms)
        for trigger in sk.triggers:
            tl = trigger.lower()
            if any(t in tl or tl in query.lower() for t in terms):
                score += 3
        if score:
            rows.append((sk, score))
    rows.sort(key=lambda x: (-x[1], x[0].id))
    return rows[:limit]


def render_list(skills: list[Skill] | None = None) -> str:
    skills = skills if skills is not None else list_skills()
    if not skills:
        return "（暂无 skills）"
    lines = []
    for sk in skills:
        triggers = ",".join(sk.triggers[:4])
        lines.append(f"{sk.id:<36} {sk.scope:<7} {sk.title}  [{triggers}]")
    return "\n".join(lines)


def render_skill(sk: Skill, include_knowledge: bool = True) -> str:
    lines = [
        f"# {sk.title}",
        "",
        f"- id: {sk.id}",
        f"- scope: {sk.scope}",
        f"- domain: {sk.domain}",
        f"- version: {sk.version}",
        f"- triggers: {', '.join(sk.triggers) or '-'}",
        f"- tools: {', '.join(sk.tools) or '-'}",
        f"- knowledge: {', '.join(sk.knowledge_ids) or '-'}",
        "",
        sk.body.strip(),
    ]
    if include_knowledge and sk.knowledge_ids:
        lines.append("")
        lines.append("## Linked Knowledge")
        for kid in sk.knowledge_ids:
            card = knowledge.get_card(kid)
            if card:
                source = f" · {card.get('source_url')}" if card.get("source_url") else ""
                lines.append(f"- {kid}: {card['title']} [{card['source_type']}]{source}")
            else:
                lines.append(f"- {kid}: missing")
    return "\n".join(lines).strip()


def context_for_query(query: str, limit: int = 2, max_chars: int = 1800) -> tuple[str, list[str]]:
    hits = search(query, limit=limit)
    if not hits:
        return "", []
    ids = []
    parts = []
    for sk, score in hits:
        ids.append(sk.id)
        body = sk.body.strip()
        if len(body) > 700:
            body = body[:700].rstrip() + "\n..."
        parts.append(f"[skill:{sk.id} score={score}] {sk.title}\n{body}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n..."
    return text, ids


def render_search(query: str, limit: int = 8) -> str:
    hits = search(query, limit=limit)
    if not hits:
        return "（无匹配 skill）"
    lines = []
    for sk, score in hits:
        lines.append(f"- {sk.id} · {sk.title} [{sk.scope}] score={score}\n  {sk.description}")
    return "\n".join(lines)
