"""分类记忆文件：一事一文件 + 索引层 + 冲突消解。

**为什么是文件而不是又一张表**：`memory.db` 的历史头号坑就是索引漂移——用户手改
markdown 不进 FTS、重装后 db 丢了而文件还在，于是"文件里明明有、recall 却说没有"。
这里把**文件本身当成唯一真相**，检索直接扫文件（语料是几百条量级，几毫秒），
根本不存在"索引和内容不一致"这种状态。用可靠性换那点速度，非常值。

**两层检索**（这是省 token 的关键）：
- 索引层：每条记忆一行 `[分类/名字] 一句话描述`，**全量注入** system prompt，几百字而已；
- 正文层：只有被点名的那几条才 `memory_read` 取全文。
筛选发生在索引上，不在正文上——把全部正文喂给模型让它筛，库越大越慢越贵，
和"省时间、减检索量"的目标正好相反。

**冲突消解**：写入走 add/update/delete/noop 四种操作（Mem0 那套）。`add` 会先查重，
命中相近记忆就**拒绝并让调用方改用 update**——默认合并优先于新建，否则同一件事
今天叫"广告优化"明天叫"ACoS调优"，越用越碎。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, textseg

# 固定一级分类。刻意不让模型自由发明分类名——自由分类必然漂移，
# 同一件事换个说法就落到新分类里，最后谁也找不着。
CATEGORIES = {
    "user": "用户是谁：身份、角色、长期偏好",
    "feedback": "用户对你工作方式的指正与认可（含原因）",
    "project": "在做的事、目标、约束（相对日期一律转成绝对日期）",
    "reference": "外部资源指针：链接、看板、工单、文档位置",
    "domain": "亚马逊运营打法、账户规律、可复用结论",
}

# 查重阈值：新记忆与已有记忆的 token 重合度超过它就判为"说的是同一件事"。
# 0.55 是保守选择——宁可偶尔提示"疑似重复"让模型确认，也不要放任碎片化。
DUPLICATE_THRESHOLD = 0.55

# 索引层注入上限。索引本身要足够小才有意义；超了就按更新时间截断，
# 老记忆仍然可以被 memory_search 检索到，只是不再免费常驻上下文。
MAX_INDEX_CHARS = 3000

# 文件名里绝不能出现的字符（Windows 比 Linux 严格，按 Windows 取交集）。
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def mem_dir() -> Path:
    return config.IVYEA_DIR / "memory"


def slugify(name: str) -> str:
    """记忆名 → 安全文件名。**保留中文**：名字必须是人能开口叫出来的，
    否则"更新一下领星广告那条记忆"这种指令就没法落地（这正是本方案的核心交互）。
    """
    s = _UNSAFE.sub("", (name or "").strip())
    s = re.sub(r"\s+", "-", s).strip("-. ")
    return s[:80] or "untitled"


def entry_path(name: str, category: str) -> Path:
    return mem_dir() / category / f"{slugify(name)}.md"


# ── frontmatter：手写解析，不引 pyyaml（CLI 不为一个元数据块加依赖）────────────
def _parse_front(text: str) -> Tuple[Dict[str, str], str]:
    """解析 `---` 包裹的 key: value 块。解析不出来就当作没有 frontmatter 的纯正文，
    绝不抛异常——用户手改文件写坏了格式，代价应该是元数据缺失，而不是整个记忆系统挂掉。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: Dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, parts[2].lstrip("\n")


def _render_front(meta: Dict[str, str], body: str) -> str:
    lines = ["---"]
    for k in ("name", "description", "category", "keywords", "links", "created", "updated", "sources"):
        if meta.get(k):
            lines.append(f"{k}: {meta[k]}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")   # encoding 显式：Windows 默认 GBK 写中文会炸
    os.replace(str(tmp), str(path))


class Entry:
    """一条记忆。刻意做成薄包装：文件是真相，这只是读出来的视图。"""

    __slots__ = ("name", "category", "description", "keywords", "links",
                 "created", "updated", "body", "path")

    def __init__(self, path: Path, meta: Dict[str, str], body: str):
        self.path = path
        self.category = meta.get("category") or path.parent.name
        self.name = meta.get("name") or path.stem
        self.description = meta.get("description", "")
        self.keywords = meta.get("keywords", "")
        self.links = meta.get("links", "")
        self.created = meta.get("created", "")
        self.updated = meta.get("updated", "")
        self.body = body

    @property
    def header_text(self) -> str:
        """检索时权重更高的部分：名字 + 描述 + 关键词。"""
        return f"{self.name} {self.description} {self.keywords}"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "category": self.category, "description": self.description,
                "keywords": self.keywords, "links": self.links, "created": self.created,
                "updated": self.updated, "body": self.body, "path": str(self.path)}

    def index_line(self) -> str:
        desc = self.description or (self.body.strip().splitlines() or [""])[0][:60]
        return f"- [{self.category}/{self.name}] {desc}"


def _load_file(path: Path) -> Optional[Entry]:
    try:
        meta, body = _parse_front(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return Entry(path, meta, body)


def list_entries() -> List[Entry]:
    """全部记忆，按更新时间倒序。直接扫目录——没有索引就没有索引漂移。"""
    root = mem_dir()
    if not root.exists():
        return []
    out: List[Entry] = []
    for cat_dir in sorted(root.iterdir()):
        if not cat_dir.is_dir():
            continue
        for f in sorted(cat_dir.glob("*.md")):
            e = _load_file(f)
            if e:
                out.append(e)
    out.sort(key=lambda e: e.updated or "", reverse=True)
    return out


def get(name: str, category: str = "") -> Optional[Entry]:
    """按名字取一条。不给分类就跨分类找——用户说"更新领星那条"时不会报分类。"""
    slug = slugify(name)
    cats = [category] if category else list(CATEGORIES)
    for cat in cats:
        p = mem_dir() / cat / f"{slug}.md"
        if p.exists():
            return _load_file(p)
    # 名字不完全一致时退化为不区分大小写的模糊匹配，模型偶尔会记错大小写或多写一个词
    low = slug.lower()
    for e in list_entries():
        if slugify(e.name).lower() == low:
            return e
    return None


# ── 检索 ────────────────────────────────────────────────────────────────────
def search(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """在分类记忆里检索。名字/描述/关键词的权重是正文的 2 倍——
    记忆的"标题"是人为提炼过的，比正文里的零散措辞更能代表这条记忆讲什么。"""
    if not textseg.tokenize(query or ""):
        return []
    scored: List[Tuple[float, Entry]] = []
    for e in list_entries():
        s = 2.0 * textseg.overlap_score(query, e.header_text) + textseg.overlap_score(query, e.body)
        if s > 0:
            scored.append((s, e))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [{**e.to_dict(), "score": round(s, 3)} for s, e in scored[:limit]]


def find_similar(text: str, exclude: str = "") -> Optional[Tuple[float, Entry]]:
    """查重：找出和给定文本讲同一件事的已有记忆。用于 add 前的合并优先判断。"""
    best: Optional[Tuple[float, Entry]] = None
    ex = slugify(exclude).lower() if exclude else ""
    for e in list_entries():
        if ex and slugify(e.name).lower() == ex:
            continue
        s = max(textseg.overlap_score(text, e.header_text),
                textseg.overlap_score(text, f"{e.header_text} {e.body}"))
        if best is None or s > best[0]:
            best = (s, e)
    if best and best[0] >= DUPLICATE_THRESHOLD:
        return best
    return None


def index_digest(limit: int = MAX_INDEX_CHARS) -> str:
    """索引层：每条一行，全量注入 system prompt。

    这是整个方案省 token 的核心——模型看着这份目录就知道"记忆里有什么"，
    需要哪条再 memory_read 取正文，而不是把所有正文都塞进上下文。
    """
    entries = list_entries()
    if not entries:
        return ""
    by_cat: Dict[str, List[Entry]] = {}
    for e in entries:
        by_cat.setdefault(e.category, []).append(e)
    parts: List[str] = []
    used = 0
    truncated = 0
    for cat in CATEGORIES:
        items = by_cat.get(cat)
        if not items:
            continue
        chunk = [f"## {cat}"]
        for e in items:
            line = e.index_line()
            if used + len(line) > limit:
                truncated += 1
                continue
            chunk.append(line)
            used += len(line)
        if len(chunk) > 1:
            parts.append("\n".join(chunk))
    if truncated:
        parts.append(f"（另有 {truncated} 条较早的记忆未列出，用 memory_search 检索）")
    return "\n".join(parts)


# ── 写入：add / update / delete / noop ───────────────────────────────────────
def apply(operation: str, name: str = "", content: str = "", category: str = "",
          description: str = "", keywords: str = "", links: str = "") -> Dict[str, Any]:
    """记忆写入的唯一入口，四种操作对应 Mem0 那套冲突消解。

    - `add`    新建。**会先查重**，命中相近记忆则拒绝并指名让你改用 update；
    - `update` 覆盖同名记忆的正文（不存在则报错，不会静默变成新建——
               静默新建正是碎片化的来源）；
    - `delete` 删掉一条（事实被推翻时用，比留着矛盾的两条强）；
    - `noop`   明确表示"这次没有值得沉淀的东西"，让不写入也成为一个显式决定。
    """
    op = (operation or "").strip().lower()
    if op == "noop":
        return {"ok": True, "operation": "noop", "message": "本次无需更新记忆。"}

    if op == "delete":
        e = get(name, category)
        if not e:
            return {"ok": False, "message": f"没有找到名为 {name!r} 的记忆。"}
        try:
            Path(e.path).unlink()
        except OSError as err:
            return {"ok": False, "message": f"删除失败：{err}"}
        return {"ok": True, "operation": "delete", "message": f"已删除记忆 [{e.category}/{e.name}]。"}

    if op not in ("add", "update"):
        return {"ok": False, "message": f"未知操作 {operation!r}，可选：add / update / delete / noop"}

    name = (name or "").strip()
    content = (content or "").strip()
    if not name:
        return {"ok": False, "message": "name 为空：记忆需要一个人能开口叫出来的名字。"}
    if not content:
        return {"ok": False, "message": "content 为空：没有可写入的内容。"}

    existing = get(name, category)

    if op == "add":
        if existing:
            return {"ok": False, "operation": "add",
                    "message": (f"已存在同名记忆 [{existing.category}/{existing.name}]。"
                                "改用 operation=update 覆盖，或换一个更具体的名字。")}
        sim = find_similar(f"{name} {description} {content}")
        if sim:
            score, e = sim
            return {"ok": False, "operation": "add", "similar_to": e.name,
                    "message": (f"疑似与已有记忆 [{e.category}/{e.name}] 讲同一件事"
                                f"（重合度 {score:.0%}）：{e.description or e.index_line()}。"
                                "先 memory_read 看一眼——同一件事请用 operation=update 合并进那条，"
                                "确属不同的事再换个更具体的名字重新 add。")}
        cat = (category or "").strip()
        if cat not in CATEGORIES:
            return {"ok": False,
                    "message": f"category 必须是以下之一：{', '.join(f'{k}({v})' for k, v in CATEGORIES.items())}"}
    else:  # update
        if not existing:
            return {"ok": False, "operation": "update",
                    "message": f"没有找到名为 {name!r} 的记忆，无法 update。要新建请用 operation=add。"}
        cat = existing.category

    today = time.strftime("%Y-%m-%d")
    meta = {
        "name": name,
        "description": (description or (existing.description if existing else "")).strip(),
        "category": cat,
        "keywords": (keywords or (existing.keywords if existing else "")).strip(),
        "links": (links or (existing.links if existing else "")).strip(),
        # created 保留原值：更新一条记忆不该抹掉它是什么时候第一次被记住的
        "created": (existing.created if existing else today) or today,
        "updated": today,
    }
    path = entry_path(name, cat)
    _atomic_write(path, _render_front(meta, content))

    # 改名/换分类的残留清理：update 时若旧文件在别的分类下，删掉避免同名两份
    if existing and Path(existing.path) != path:
        try:
            Path(existing.path).unlink()
        except OSError:
            pass

    verb = "已新建" if op == "add" else "已更新"
    return {"ok": True, "operation": op, "name": name, "category": cat, "path": str(path),
            "message": f"{verb}记忆 [{cat}/{name}]。"}


def stats() -> Dict[str, Any]:
    entries = list_entries()
    by_cat: Dict[str, int] = {}
    for e in entries:
        by_cat[e.category] = by_cat.get(e.category, 0) + 1
    return {"total": len(entries), "by_category": by_cat, "dir": str(mem_dir()),
            "index_chars": len(index_digest())}
