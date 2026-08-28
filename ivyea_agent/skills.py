"""Reusable Ivyea skills.

A skill is a small, versioned operating playbook that can be loaded into an
agent turn or run from the CLI. Built-in skills live in package data; personal
skills live in ``~/.ivyea/skills``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
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


#: 归档目录名。它是技能库的**子目录**（`~/.ivyea/skills/_archive/`），所以扫描时必须
#: 显式跳过 —— 否则归档过的技能照样被加载，"归档"就成了一个纯粹的目录搬家动作。
ARCHIVE_DIRNAME = "_archive"


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
        if ARCHIVE_DIRNAME in rel.parts:
            continue                       # 归档区就在技能库里面；不跳过等于"归档了个寂寞"
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



# ── 写入 / 归档 / 读附属文件 ──────────────────────────────────────────────────
#
# 在这之前，建技能只有一条路：人去敲 `ivyea skill create`。模型手上只有 `skill_search`
# 一个只读工具 —— 它可以刚刚走完一套完整流程，然后眼睁睁看着这套流程随会话消失。
# 下面这几个函数是"agent 自己能沉淀技能"的落地面，写操作一律经审批（在工具层把关）。

def user_skill_dir(skill_id: str) -> Path:
    """用户技能的落盘目录。id 里的点被当成 domain/name 分隔（与 create_user_skill 一致）。"""
    sid = (skill_id or "").strip()
    if not re.match(r"^[a-zA-Z0-9_.-]+$", sid):
        raise ValueError("skill id 只能包含字母、数字、点、下划线和短横线")
    domain, _, name = sid.rpartition(".")
    return _user_base() / (domain or "user") / (name or sid)


def write_user_skill(skill_id: str, meta: dict[str, Any], body: str,
                     *, overwrite: bool = True) -> Path:
    """写一份 SKILL.md（frontmatter 格式，见 ADR-0009）。返回文件路径。

    **不做校验** —— 校验在 `skill_authoring.validate`，由调用方（工具层/CLI）先跑、
    不过就不该走到这里。这里只负责落盘，职责单一。
    """
    from . import skill_authoring
    path = user_skill_dir(skill_id)
    target = path / "SKILL.md"
    if target.exists() and not overwrite:
        raise FileExistsError(f"skill 已存在：{target}")
    path.mkdir(parents=True, exist_ok=True)
    payload = dict(meta or {})
    # id 显式写死：加载器没有 id 就从 name 推导，推出来的和调用方给的往往不是一回事
    # （name=lingxing-ad-patrol → id=lingxing.lingxing_ad_patrol），后续 view/archive 全找不着。
    payload["id"] = skill_id
    payload.setdefault("name", skill_id.rpartition(".")[2] or skill_id)
    payload.setdefault("version", "0.1.0")
    domain = skill_id.rpartition(".")[0]
    if domain:
        payload.setdefault("domain", domain)
    target.write_text(skill_authoring.render_frontmatter(payload, body), encoding="utf-8")
    return target


def write_skill_asset(skill_id: str, rel_path: str, content: str) -> Path:
    """往技能目录里写一个附属文件（scripts/ references/ templates/）。

    路径必须**留在技能目录内** —— 它来自模型，`../../` 一路能写到任何地方。
    """
    base = user_skill_dir(skill_id).resolve()
    rel = (rel_path or "").strip().lstrip("/\\")
    if not rel or rel in ("SKILL.md", "skill.json"):
        raise ValueError("附属文件名不能为空，也不能是 SKILL.md / skill.json（正文用 write 动作写）")
    target = (base / rel).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"附属文件必须留在技能目录内：{rel_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content or "", encoding="utf-8")
    return target


def archive_skill(skill_id: str) -> Path:
    """归档一个用户技能（移进 `_archive/`）。**只归档不删除**，随时可恢复。"""
    sk = get_skill(skill_id)
    if sk is None:
        raise FileNotFoundError(f"未找到 skill：{skill_id}")
    if sk.scope != "user":
        raise ValueError(f"只能归档用户自建技能；{skill_id} 是 {sk.scope} 技能。")
    src = Path(sk.path)
    dest = _user_base() / ARCHIVE_DIRNAME / f"{skill_id}-{int(time.time())}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def restore_skill(archive_name: str) -> Path:
    """把归档里的技能放回去。archive_name 是 `_archive/` 下的目录名。"""
    src = _user_base() / ARCHIVE_DIRNAME / archive_name
    if not src.is_dir():
        raise FileNotFoundError(f"归档里没有：{archive_name}")
    skill_id = archive_name.rsplit("-", 1)[0]
    dest = user_skill_dir(skill_id)
    if dest.exists():
        raise FileExistsError(f"目标已存在，先处理掉：{dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def list_archive() -> list[str]:
    base = _user_base() / ARCHIVE_DIRNAME
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def list_assets(sk: Skill) -> list[str]:
    """技能目录下除 SKILL.md / skill.json 之外的文件（相对路径）。"""
    try:
        base = Path(sk.path)
        return sorted(
            p.relative_to(base).as_posix() for p in base.rglob("*")
            if p.is_file() and p.name not in ("SKILL.md", "skill.json")
        )
    except Exception:      # noqa: BLE001
        return []


def read_asset(sk: Skill, rel_path: str, max_chars: int = 20000) -> str:
    """读技能目录下的一个附属文件。路径必须留在技能目录内。"""
    base = Path(sk.path).resolve()
    target = (base / (rel_path or "").strip().lstrip("/\\")).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"路径超出技能目录：{rel_path}")
    if not target.is_file():
        raise FileNotFoundError(f"技能目录里没有这个文件：{rel_path}")
    text = target.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= max_chars else text[:max_chars] + "\n…（已截断）"

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

# 中文查询是按 2-gram 切的，于是"为什么我的模型配置页面报错"会切出「为什」「什么」，
# 而这两个 2-gram 正好落在技能触发词「**为什么**卖不好」里 —— 一句和亚马逊毫无关系
# 的话就这样以 22 分命中了 ASIN 审计手册。用户的原话是"不管什么问题，任务台第一句
# 好多都是：匹配最合适的技能 ✦ Amazon ASIN COSMO"。
#
# 这些片段是**疑问词/人称/礼貌语**，它们出现在哪条技能里都不说明任何事，所以不参与
# 打分。只挡 2-gram 这一层：正文分本来就不进名义分，人工检索也不受影响。
_STOP_GRAMS = frozenset("""
为什 什么 怎么 么办 如何 是否 可以 能否 需要 应该 这个 那个 这些 那些 一下 一个
我的 我们 你的 你们 他的 它的 帮我 帮忙 请问 麻烦 谢谢 你好 现在 目前 已经 还有
问题 情况 时候 之后 之前 上面 下面 里面 外面 出现 发生 导致 造成 提示 显示 告诉
不能 不了 没有 无法 不对 不行 怎样 多少 哪些 哪个 什麼 為什
""".split())


def _is_noise(gram: str) -> bool:
    """这个片段是不是"出现在哪都不说明任何事"的通用词。

    只对**两字片段**生效：更长的片段是用户真的打出来的词（"广告"" ASIN"），
    再通用也是他自己选的词；两字片段则大半是切出来的碎渣。
    """
    return len(gram) == 2 and gram in _STOP_GRAMS


def _score_parts(sk: "Skill", terms: list[str], raw_terms: list[str], ql: str) -> tuple[int, int]:
    """返回 (总分, 名义分)。

    名义分 = 命中了这条技能的 **id / 标题 / 描述 / 触发词**，也就是"这条技能就是
    干这个的"。正文分只算进总分：正文动辄几千字，任意常用词都能在里面撞上几次 ——
    一句「测试」能以 score=2 命中 ASIN 审计手册，靠的全是正文里出现过两次"测试"。
    """
    meta = " ".join([sk.id, sk.title, sk.description, " ".join(sk.triggers)]).lower()
    # 名义分只认**有信息量的片段**：通用疑问词/人称撞上触发词是噪音，不是命中。
    named = _META_WEIGHT * sum(min(meta.count(t), _META_HIT_CAP)
                               for t in terms if not _is_noise(t))
    bonus = 0
    for trigger in sk.triggers:
        tl = trigger.lower()
        if any(t in tl or tl in ql for t in raw_terms if not _is_noise(t)):
            bonus += _TRIGGER_BONUS
    named += min(bonus, _TRIGGER_BONUS_CAP)
    total = named + sum(min(sk.body.lower().count(t), _BODY_HIT_CAP)
                        for t in terms if not _is_noise(t))
    return total, named


# 自动注入要求的名义分下限。一个 _META_WEIGHT(3) 的单点命中不够 —— 那种大多是
# 某个常用词恰好也出现在标题里；要求两处以上才认。人工检索不设这道闸。
_AUTO_NAMED_MIN = 6


def _semantic_text(sk: Skill) -> str:
    """喂给向量的文本。**只用元信息，不用正文** —— 正文动辄几千字，一段技能手册的
    向量会被大量流程细节主导，"这技能是干什么的"反而被稀释。标题/描述/触发词才是
    作者对这件事的表述。"""
    return " ".join(filter(None, [sk.title, sk.description, " ".join(sk.triggers), sk.id]))


def search(query: str, limit: int = 8, *, named_only: bool = False,
           semantic: bool | None = None) -> list[tuple[Skill, int]]:
    """按相关度排序的技能。

    `named_only=True` 只保留**名义命中**（标题/描述/触发词对上）的那些 —— 自动注入
    走这条，见 _score_parts 的说明。人工检索（skill search）不设这道闸：那时候用户
    是在翻库，宁可多给几条。

    语义层（`semantic`）
    -------------------
    词法这条路是 2-gram + 一张手工停用词表，它的失败模式在 `_STOP_GRAMS` 上面记着：
    一句和亚马逊毫无关系的话曾以 22 分命中 ASIN 审计手册。反过来也一样 —— 口语化的
    问法（"这个类目还能不能做"）和技能里的正式用词对不上，词法就是零命中。

    所以接上记忆那套已经验证过的双路召回（`memory_vectors.hybrid_rank`，RRF 融合）。
    两条纪律：

    * **候选集是全部技能，不是词法命中的那些。** 把语义做成"重排词法候选"是错的 ——
      词法零命中时候选集是空的，语义根本没有机会，而那正是它唯一存在的理由
      （`memory_vectors.vector_recall` 的注释里记着这个坑）。
    * **`named_only` 那道闸不因语义放松。** 自动注入的误报是有过真实事故的，
      语义只用来**排序**已经过闸的那些；发现新技能是 `skill_search` 的事。
      没有 dense 后端时，一切行为与纯词法**逐条相同**。
    """
    terms = _terms(query)
    if not terms:
        return []
    # 触发词加分只认**原始词**，不认切出来的 2-gram：任意两个字都能落进某条触发词里，
    # 那样加分就成了噪音（"帮我分析"里的"分析"能把一堆技能全拉进来）。
    raw_terms = [t.lower() for t in re.findall(r"[\w一-鿿+.-]+", query)]
    ql = query.lower()
    all_skills = list_skills()
    scored: dict[str, int] = {}
    admitted: list[Skill] = []
    for sk in all_skills:
        score, named = _score_parts(sk, terms, raw_terms, ql)
        if not score or (named_only and named < _AUTO_NAMED_MIN):
            continue
        scored[sk.id] = score
        admitted.append(sk)
    admitted.sort(key=lambda x: (-scored[x.id], x.id))

    if semantic is None:
        semantic = bool(config.get_setting("skill_semantic_search", True))
    if not semantic:
        return [(sk, scored[sk.id]) for sk in admitted[:limit]]

    # 自动注入：候选集就是过了闸的那些，语义只负责排序。
    # 人工/模型检索：候选集是全部技能，语义有机会捞出词法零命中的那条。
    pool = admitted if named_only else all_skills
    if not pool:
        return []
    index = {id(sk): i for i, sk in enumerate(pool)}
    lex_ranked = [index[id(sk)] for sk in admitted if id(sk) in index]
    try:
        from . import memory_vectors
        ranked = memory_vectors.hybrid_rank(query, pool, _semantic_text,
                                            limit=limit, lex_ranked=lex_ranked)
    except Exception:      # noqa: BLE001 —— 语义层出任何问题都退回纯词法，不能让检索挂掉
        return [(sk, scored[sk.id]) for sk in admitted[:limit]]
    # 纯语义捞出来的（词法 0 分）给 1 分，好和"词法命中"区分得开。
    return [(sk, scored.get(sk.id, 1)) for sk in ranked]


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


#: 自动注入时每条技能给多少正文。**它不再是"手册全文被截断后的残骸"** —— 见下面
#: context_for_query 的说明。
_INJECT_BODY_CHARS = 700


def context_for_query(query: str, limit: int = 2, max_chars: int = 1800) -> tuple[str, list[str]]:
    """自动注入用的技能上下文。**只认名义命中**（named_only）。

    这里注入的东西会实打实地改变模型的行为 —— 只靠正文撞词命中就注入，等于给一句
    「测试」派了一份 ASIN 审计流程。所以准入闸不动。

    渐进披露
    --------
    此前这里把正文**截到 700 字**塞进去，而剩下的部分模型**根本够不着** —— 没有任何
    工具能读技能全文。于是一本三千字的审计手册，模型永远只看得到开头那段"何时使用"，
    真正的步骤和护栏全在截断线以下。它照着残缺的手册干活，还以为自己看全了。

    现在改成：注入**开头一段 + 一句"要全文用 `skill_view`"**，把"读多少"的决定权
    交还给模型。截断这件事本身没变（上下文是有价的），变的是**截掉的部分现在拿得回来**。
    """
    hits = search(query, limit=limit, named_only=True)
    if not hits:
        return "", []
    ids = []
    parts = []
    for sk, score in hits:
        ids.append(sk.id)
        body = sk.body.strip()
        truncated = len(body) > _INJECT_BODY_CHARS
        if truncated:
            body = body[:_INJECT_BODY_CHARS].rstrip() + "\n…"
        # 提示写在正文**之前**：正文会被截断，跟在后面的说明进不了上下文。
        hint = ""
        if truncated:
            hint = f"\n（以上只是开头。完整步骤用 `skill_view(skill_id=\"{sk.id}\")` 读全文——"
            hint += "下面这段被截断了，不要凭这一小段就动手。）"
        assets = list_assets(sk)
        if assets:
            shown = "、".join(assets[:6]) + ("…" if len(assets) > 6 else "")
            hint += (f"\n（这个技能还带了附属文件：{shown}。"
                     f"用 `skill_view(skill_id=\"{sk.id}\", file_path=\"…\")` 按需读。）")
        parts.append(f"[skill:{sk.id} score={score}] {sk.title}{hint}\n{body}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…"
    try:
        from . import skill_usage
        skill_usage.record(ids, query=query, source="inject")
    except Exception:      # noqa: BLE001 —— 统计坏了不该连累注入
        pass
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
