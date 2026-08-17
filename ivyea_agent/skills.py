"""Reusable Ivyea skills.

A skill is a small, versioned operating playbook that can be loaded into an
agent turn or run from the CLI. Built-in skills live in package data; personal
skills live in ``~/.ivyea/skills``.
"""
from __future__ import annotations

import json
import os
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


# ── SKILL.md + YAML frontmatter ─────────────────────────────────────────────
#
# **这是业界通行的写法**（Anthropic Agent Skills / Claude Code 就是它）：元数据和
# 正文在同一个文件里，附属的脚本、参考文档放在同目录按需读取。本仓早期用的是
# 「SKILL.md + 旁边一个 skill.json」，等于给同一件事发明了第二种格式 —— 结果是
# 外部技能库（比如 IvyeaOps 的 Skill 中心，近百个技能）一个都加载不进来，只能靠
# 上游把正文抄进 system 上下文，附属文件全丢。
#
# 两种格式都认：skill.json 在就按它（老技能一个不动），不在就读 frontmatter。

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """(frontmatter, body)。没有 frontmatter 或解析失败都返回 ({}, 原文)。

    用真的 YAML 解析器而不是手写正则：这些 description 里有冒号、中文标点和引号，
    手写解析器在这种输入上出错是必然的，而且错得很安静。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        import yaml  # 见 pyproject：为这条能力显式声明的依赖
    except ImportError:
        # 老环境升级上来可能还没装。跳过 frontmatter 技能，**别把整个加载器带崩** ——
        # skill.json 那批必须照常工作。
        return {}, m.group(2)
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}, m.group(2)
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def _first_str(fm: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = fm.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _fm_triggers(fm: dict[str, Any]) -> list[str]:
    """作者声明的 `triggers:` 优先；没有就退回 `metadata.hermes.tags`。

    中文查询几乎完全靠这些**短词**：`_terms()` 不分词，一整句中文会被当成一个词，
    只有 trigger 作为子串命中才拿得到分。
    """
    out: list[str] = []
    for value in (fm.get("triggers"), ((fm.get("metadata") or {}).get("hermes") or {}).get("tags")
                  if isinstance(fm.get("metadata"), dict) else None):
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, str):
                    continue
                # 作者常把一行写成「调研报告、选品调研、市场分析」这样的一串。不切开的话
                # 它就是一条长得离谱的"触发词"，既匹配不准（"帮我分析"这种口语也在里面），
                # 又会把不相干的查询拉进来。按中英文列表分隔符拆成本来的那几个词。
                for piece in re.split(r"[、，,;；/|]+", item):
                    piece = piece.strip()
                    if piece and piece not in out:
                        out.append(piece)
        if out:
            break
    return out


def _load_frontmatter_skill(path: Path, scope: str, domain: str) -> Skill | None:
    try:
        text = path.joinpath("SKILL.md").read_text(encoding="utf-8")
    except Exception:
        return None
    fm, body = _parse_frontmatter(text)
    if not fm:
        return None
    name = _first_str(fm, "name") or path.name
    dom = _first_str(fm, "domain") or domain
    skill_id = _first_str(fm, "id") or (f"{dom}.{_slug(name)}" if dom else _slug(name))
    # description_zh 优先：这套东西面向中文用户，描述会进匹配的 haystack。
    desc = _first_str(fm, "description_zh", "description")
    return Skill(
        id=skill_id,
        title=_first_str(fm, "title") or name,
        domain=dom,
        version=_first_str(fm, "version"),
        description=desc,
        triggers=_fm_triggers(fm),
        knowledge_ids=[k for k in (fm.get("knowledge_ids") or []) if isinstance(k, str)],
        tools=[t for t in (fm.get("tools") or []) if isinstance(t, str)],
        path=str(path),
        scope=scope,
        body=body,
    )


def _load_skill(path: Path, scope: str, domain: str = "") -> Skill | None:
    """一个技能目录 → Skill。skill.json 优先，其次 SKILL.md frontmatter。"""
    return _load_manifest(path, scope) or _load_frontmatter_skill(path, scope, domain)


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


def _iter_root(base: Path, scope: str) -> list[Skill]:
    """扫一个技能库根目录。**按 SKILL.md 找**（它是两种格式都有的那个文件）。

    domain 取相对根目录的第一段目录名（`amazon/xxx/SKILL.md` → `amazon`）；
    技能直接放在根下时退回根目录自己的名字，这样把 `.../skills/amazon` 整个
    当作一个库挂上来也能得到正确的 domain。
    """
    rows: list[Skill] = []
    if not base.exists():
        return rows
    for skill_md in sorted(base.rglob("SKILL.md")):
        d = skill_md.parent
        try:
            rel = d.relative_to(base)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue                       # .archive / .git 之类的不算技能
        domain = rel.parts[0] if len(rel.parts) > 1 else base.name
        sk = _load_skill(d, scope, domain)
        if sk:
            rows.append(sk)
    return rows


def _iter_user() -> list[Skill]:
    return _iter_root(_user_base(), "user")


def _extra_roots() -> list[Path]:
    """外部技能库。让 IvyeaOps 这类上游把自己的技能库**原地**挂上来。

    以前上游只能把技能复制一份并转换成 skill.json 才能被加载；那层转换现在不需要了 ——
    格式已经通用，目录直接挂。配置两种来源，环境变量优先：

      IVYEA_SKILL_ROOTS=/a/skills:/b/skills   （Windows 用 ; 分隔，跟 PATH 一致）
      settings.json 里的 "skill_roots": [...]
    """
    raw: list[str] = []
    env = os.environ.get("IVYEA_SKILL_ROOTS", "")
    if env.strip():
        raw = [p for p in env.split(os.pathsep) if p.strip()]
    else:
        value = config.get_setting("skill_roots", []) or []
        if isinstance(value, str):
            value = [value]
        raw = [str(p) for p in value if str(p).strip()]
    roots: list[Path] = []
    for p in raw:
        try:
            path = Path(p).expanduser().resolve()
        except Exception:
            continue
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _iter_extra() -> list[Skill]:
    rows: list[Skill] = []
    for root in _extra_roots():
        rows.extend(_iter_root(root, "external"))
    return rows


def list_skills(include_user: bool = True) -> list[Skill]:
    """内置技能，被同 id 的个人技能覆盖；外部技能库只填空位。

    **外部库不许覆盖内置技能。** 上游（比如 IvyeaOps 的 Skill 中心）挂上来的目录里
    随手建一个同名技能就把内置技能顶掉，是很难查的故障：表现为"内置技能突然换了套
    说法"，而两边看起来都正常。个人技能（~/.ivyea/skills）保持原有的覆盖语义 ——
    那是本机作者的明确意图。
    """
    by_id: dict[str, Skill] = {}
    for sk in _iter_builtin():
        by_id[sk.id] = sk
    if include_user:
        for sk in _iter_user():
            by_id[sk.id] = sk
        for sk in _iter_extra():
            by_id.setdefault(sk.id, sk)
    return sorted(by_id.values(), key=lambda s: (s.domain, s.id))


def inventory() -> dict[str, list[Skill]]:
    """Return every skill variant grouped by id, without applying overrides."""
    rows: dict[str, list[Skill]] = {}
    for sk in _iter_builtin() + _iter_user():
        rows.setdefault(sk.id, []).append(sk)
    for variants in rows.values():
        variants.sort(key=lambda s: (s.scope != "builtin", s.path))
    return dict(sorted(rows.items()))


def _version_key(value: str) -> tuple[int, tuple[int, ...], str]:
    raw = (value or "").strip().lower().lstrip("v")
    if raw in ("", "local"):
        return (0, (), raw)
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", raw)
    if not m:
        return (0, (), raw)
    return (1, tuple(int(p) for p in m.group(1).split(".")), m.group(2))


def compare_versions(left: str, right: str) -> int:
    lk = _version_key(left)
    rk = _version_key(right)
    if lk == rk:
        return 0
    return 1 if lk > rk else -1


def status() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for skill_id, variants in inventory().items():
        builtin = next((s for s in variants if s.scope == "builtin"), None)
        user = next((s for s in variants if s.scope == "user"), None)
        active = user or builtin or variants[0]
        issues: list[str] = []
        if len(variants) > 1:
            issues.append("overridden_by_user" if user and builtin else "duplicate_id")
        if user and builtin:
            cmp = compare_versions(user.version, builtin.version)
            if cmp < 0:
                issues.append(f"user_version_behind_builtin:{user.version or '-'}<{builtin.version or '-'}")
            elif cmp > 0:
                issues.append(f"user_version_ahead_builtin:{user.version or '-'}>{builtin.version or '-'}")
            else:
                issues.append("user_override_same_version")
        for kid in active.knowledge_ids:
            if not knowledge.get_card(kid):
                issues.append(f"missing_knowledge:{kid}")
        rows.append({
            "id": skill_id,
            "active_scope": active.scope,
            "active_version": active.version,
            "builtin_version": builtin.version if builtin else "",
            "user_version": user.version if user else "",
            "domain": active.domain,
            "title": active.title,
            "variant_count": len(variants),
            "path": active.path,
            "issues": issues,
            "ok": not issues or issues == ["user_override_same_version"],
        })
    return rows


def lockfile() -> dict[str, Any]:
    return {
        "version": 1,
        "generated_by": "ivyea-agent",
        "skills": [
            {
                "id": sk.id,
                "scope": sk.scope,
                "domain": sk.domain,
                "version": sk.version,
                "title": sk.title,
                "path": sk.path,
                "knowledge_ids": sk.knowledge_ids,
                "tools": sk.tools,
                "triggers": sk.triggers,
            }
            for sk in list_skills()
        ],
    }


def write_lockfile(path: str | Path | None = None) -> Path:
    out = Path(path).expanduser() if path else _user_base() / "skills.lock.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lockfile(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def create_user_skill(
    skill_id: str,
    title: str = "",
    domain: str = "",
    description: str = "",
    triggers: list[str] | None = None,
    tools: list[str] | None = None,
    knowledge_ids: list[str] | None = None,
    body: str = "",
    overwrite: bool = False,
) -> Skill:
    """Create a user skill skeleton under ~/.ivyea/skills."""
    skill_id = skill_id.strip()
    if not re.match(r"^[a-zA-Z0-9_.-]+$", skill_id):
        raise ValueError("skill id 只能包含字母、数字、点、下划线和短横线")
    domain = (domain or skill_id.split(".", 1)[0] if "." in skill_id else domain or "user").strip()
    name = skill_id.split(".")[-1]
    path = _user_base() / domain / name
    if path.exists() and not overwrite:
        raise FileExistsError(f"skill 已存在：{path}")
    path.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": skill_id,
        "title": title or skill_id,
        "domain": domain,
        "version": "local",
        "description": description or "User-defined skill.",
        "triggers": triggers or [],
        "knowledge_ids": knowledge_ids or [],
        "tools": tools or [],
    }
    default_body = f"""# {title or skill_id}

## When to use
- Describe the user request patterns that should trigger this skill.

## Workflow
1. Inspect the available context and data.
2. State assumptions and risks.
3. Produce concrete next actions.

## Guardrails
- Do not perform write operations without approval.
- Cite knowledge sources when facts matter.
"""
    path.joinpath("skill.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    path.joinpath("SKILL.md").write_text(body.strip() + "\n" if body.strip() else default_body, encoding="utf-8")
    sk = _load_manifest(path, "user")
    if not sk:
        raise RuntimeError("skill 创建后读取失败")
    return sk


def get_skill(skill_id: str) -> Skill | None:
    for sk in list_skills():
        if sk.id == skill_id:
            return sk
    return None


def audit() -> list[dict[str, Any]]:
    rows = []
    for skill_id, variants in inventory().items():
        sk = variants[-1]
        issues = []
        if len(variants) > 1:
            issues.append("overridden_by_user" if any(v.scope == "user" for v in variants) else "duplicate_id")
        if not sk.triggers:
            issues.append("missing_triggers")
        if not sk.body.strip():
            issues.append("empty_body")
        for kid in sk.knowledge_ids:
            if not knowledge.get_card(kid):
                issues.append(f"missing_knowledge:{kid}")
        rows.append({
            "id": skill_id,
            "scope": sk.scope,
            "title": sk.title,
            "path": sk.path,
            "ok": not issues,
            "issues": issues,
        })
    return rows


_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")


def _terms(query: str) -> list[str]:
    """\u628a\u67e5\u8be2\u5207\u6210\u7528\u4e8e\u6253\u5206\u7684\u8bcd\u3002

    **\u4e2d\u6587\u5fc5\u987b\u5207 2-gram\u3002** \u8fd9\u91cc\u6ca1\u6709\u5206\u8bcd\u5668\uff0c\u6b63\u5219 `[\\w\u4e00-\u9fff+.-]+` \u4f1a\u628a
    "\u505a\u4e2a\u5e02\u573a\u8c03\u7814\u770b\u770b\u8fd9\u4e2a\u7c7b\u76ee" \u6574\u4e32\u5f53\u4f5c**\u4e00\u4e2a**\u8bcd \u2014\u2014 \u5b83\u4e0d\u53ef\u80fd\u51fa\u73b0\u5728\u4efb\u4f55\u6280\u80fd\u6b63\u6587\u91cc\uff0c
    \u4e8e\u662f\u4e2d\u6587\u63d0\u95ee\u7684\u5339\u914d\u5206\u6052\u4e3a 0\uff0c\u7b49\u4e8e\u4e2d\u6587\u7528\u6237\u6839\u672c\u7528\u4e0d\u4e0a\u6280\u80fd\u5339\u914d\u3002\u4ee5\u524d\u662f\u9760\u7ed9\u6bcf\u4e2a\u6280\u80fd
    \u624b\u5de5\u914d\u4e00\u6279\u77ed\u89e6\u53d1\u8bcd\u7ed5\u8fc7\u53bb\u7684\uff0c\u90a3\u65e2\u8981\u7ef4\u62a4\u8bcd\u8868\uff0c\u53c8\u53ea\u5bf9\u914d\u8fc7\u7684\u6280\u80fd\u6709\u6548\u3002

    2-gram \u662f\u8fd9\u91cc\u6700\u5c0f\u53ef\u7528\u7684\u5206\u8bcd\u66ff\u4ee3\uff1a\u300c\u5e02\u573a\u8c03\u7814\u300d\u5207\u51fa \u5e02\u573a/\u573a\u8c03/\u8c03\u7814\uff0c\u5176\u4e2d"\u5e02\u573a"
    "\u8c03\u7814"\u6b63\u662f\u6280\u80fd\u63cf\u8ff0\u91cc\u771f\u5b9e\u51fa\u73b0\u7684\u8bcd\u3002\u566a\u97f3\uff08"\u5e2e\u6211""\u8fd9\u4e2a"\uff09\u51e0\u4e4e\u4e0d\u51fa\u73b0\u5728\u6280\u80fd\u6587\u672c\u91cc\uff0c
    \u5bf9\u6392\u5e8f\u5f71\u54cd\u53ef\u4ee5\u5ffd\u7565\u3002
    """
    out: list[str] = []

    def add(t: str) -> None:
        if t and t not in out:
            out.append(t)

    for token in re.findall(r"[\w\u4e00-\u9fff+.-]+", query.lower()):
        add(token)
    # \u4ece\u539f\u59cb\u67e5\u8be2\u91cc\u53d6\u4e2d\u6587\u4e32 \u2014\u2014 \u8fd9\u6837 "asin\u5ba1\u8ba1" \u8fd9\u79cd\u4e2d\u82f1\u6df7\u6392\u7684\u4e5f\u80fd\u5207\u5230\u3002
    for run in _CJK_RUN.findall(query):
        for i in range(len(run) - 1):
            add(run[i:i + 2])
    return out


#: 正文里同一个词命中再多也只算这么多次。
#:
#: 切了 2-gram 之后，长正文的技能会靠噪音词堆分 —— 实测一个几千字的审计技能能在
#: "写一版主图创意"这种毫不相干的查询上排到第一。标识/标题/描述才是作者对"这技能
#: 是干什么的"的表述，正文只是佐证，所以前者加权、后者封顶。
_BODY_HIT_CAP = 2
#: 标识/标题/描述里同一个词也要封顶：长中文描述里"分析""报告"这种词能出现十几次，
#: 不封顶的话"什么都沾一点"的宽泛技能会盖过真正对口的那个。
_META_HIT_CAP = 3
_META_WEIGHT = 3
_TRIGGER_BONUS = 3
_TRIGGER_BONUS_CAP = 9


def search(query: str, limit: int = 8) -> list[tuple[Skill, int]]:
    terms = _terms(query)
    if not terms:
        return []
    # 触发词加分只认**原始词**，不认切出来的 2-gram：任意两个字都能落进某条触发词里，
    # 那样加分就成了噪音（"帮我分析"里的"分析"能把一堆技能全拉进来）。
    raw_terms = [t.lower() for t in re.findall(r"[\w一-鿿+.-]+", query)]
    ql = query.lower()
    rows: list[tuple[Skill, int]] = []
    for sk in list_skills():
        meta = " ".join([sk.id, sk.title, sk.description, " ".join(sk.triggers)]).lower()
        body = sk.body.lower()
        score = _META_WEIGHT * sum(min(meta.count(t), _META_HIT_CAP) for t in terms)
        score += sum(min(body.count(t), _BODY_HIT_CAP) for t in terms)
        bonus = 0
        for trigger in sk.triggers:
            tl = trigger.lower()
            if any(t in tl or tl in ql for t in raw_terms):
                bonus += _TRIGGER_BONUS
        score += min(bonus, _TRIGGER_BONUS_CAP)
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


def has_assets(sk: Skill) -> bool:
    """这个技能目录里除了 SKILL.md 还有别的文件吗（脚本 / 参考文档 / 模板）。"""
    try:
        base = Path(sk.path)
        return any(p.is_file() and p.name not in ("SKILL.md", "skill.json")
                   for p in base.rglob("*"))
    except Exception:
        return False


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
    # 说明书里写着"运行 scripts/xxx.py""参见 references/xxx.md"，就得告诉它这些
    # 东西在哪 —— 否则它要么瞎找，要么凭正文硬编。只在真有附属文件时说。
    if has_assets(sk):
        lines[9:9] = [f"- 文件目录: {sk.path}（正文里的 scripts/ references/ 等相对路径都在这下面，可直接读取）"]
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
        # 目录写在正文**之前**：正文会被截断，跟在后面的说明进不了上下文。
        where = f"\n（技能目录：{sk.path}，正文提到的相对路径都在这下面）" if has_assets(sk) else ""
        parts.append(f"[skill:{sk.id} score={score}] {sk.title}{where}\n{body}")
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


def render_audit(rows: list[dict[str, Any]] | None = None) -> str:
    rows = rows if rows is not None else audit()
    if not rows:
        return "Skill Audit\n\n（暂无 skills）"
    lines = ["Skill Audit", ""]
    for row in rows:
        status = "OK" if row["ok"] else "WARN"
        issues = ", ".join(row["issues"]) if row["issues"] else "-"
        lines.append(f"- {status} {row['id']} [{row['scope']}] issues={issues}")
    return "\n".join(lines)


def render_status(rows: list[dict[str, Any]] | None = None) -> str:
    rows = rows if rows is not None else status()
    if not rows:
        return "Skill Status\n\n（暂无 skills）"
    lines = ["Skill Status", ""]
    for row in rows:
        issues = ", ".join(row["issues"]) if row["issues"] else "-"
        versions = []
        if row.get("builtin_version"):
            versions.append(f"builtin={row['builtin_version']}")
        if row.get("user_version"):
            versions.append(f"user={row['user_version']}")
        lines.append(
            f"- {'OK' if row['ok'] else 'WARN'} {row['id']} "
            f"active={row['active_scope']}:{row['active_version'] or '-'} "
            f"variants={row['variant_count']} {' '.join(versions)} issues={issues}"
        )
    return "\n".join(lines)
