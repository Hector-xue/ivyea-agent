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


FRONT_KEYS = ("name", "description", "category", "keywords", "links",
              "created", "updated", "sources",
              # 双时间轴与作用域（阶段 3）：
              # created/updated 是**记录时间**（我们什么时候写下这条），
              # valid_from/valid_until 是**事实时间**（这件事什么时候起作用、什么时候失效）。
              # 只有记录时间的话，"目标 ACoS 旺季 25% 淡季 35%" 这种季节性事实要么互相覆盖
              # （丢历史，答不了"什么时候改的"），要么并存成两条矛盾记忆让 agent 瞎猜。
              "valid_from", "valid_until", "scope",
              # 溯源与置信（阶段 4）：反思会**过度概括**——实测它把"用户几次在开发后要求
              # 发版"总结成了"用户习惯开发完就发版"，而真实规矩是未经批准绝不发版。
              # 证据门槛挡不住这类：统计上确实发生过，只是"发生过"不等于"是偏好"。
              # 记下支撑证据和置信度，才能回答"你凭什么这么认为"，也才有纠正的抓手。
              "confidence", "evidence", "source")


def _render_front(meta: Dict[str, str], body: str) -> str:
    lines = ["---"]
    for k in FRONT_KEYS:
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
                 "created", "updated", "body", "path", "valid_from", "valid_until", "scope",
                 "confidence", "evidence", "source")

    def __init__(self, path: Path, meta: Dict[str, str], body: str):
        self.path = path
        self.category = meta.get("category") or path.parent.name
        self.name = meta.get("name") or path.stem
        self.description = meta.get("description", "")
        self.keywords = meta.get("keywords", "")
        self.links = meta.get("links", "")
        self.created = meta.get("created", "")
        self.updated = meta.get("updated", "")
        # 缺失即"一直有效 / 全局适用"：老文件没有这些字段，绝不能因此被判为失效而消失。
        self.valid_from = meta.get("valid_from", "")
        self.valid_until = meta.get("valid_until", "")
        self.scope = meta.get("scope", "")
        # source: user(用户明说的) | reflection(反思推断的) | manual(手写文件)。
        # 默认 user 而非 reflection：老文件和用户手写的都该当作可信，
        # 只有反思**主动声明**自己是推断时才降级。
        self.source = meta.get("source", "") or "user"
        self.confidence = _clamp_conf(meta.get("confidence", ""), self.source)
        self.evidence = meta.get("evidence", "")
        self.body = body

    def is_valid_on(self, day: str = "") -> bool:
        """该记忆在某天是否有效。日期用 YYYY-MM-DD 字符串比较——ISO 格式下字典序即时间序，
        不用引 datetime 解析，也就不会因为用户手写了 '2026/8/1' 这种格式而抛异常。"""
        day = day or time.strftime("%Y-%m-%d")
        if self.valid_from and _norm_day(self.valid_from) > day:
            return False
        if self.valid_until and _norm_day(self.valid_until) < day:
            return False
        return True

    def matches_scope(self, scope: str = "") -> bool:
        """作用域匹配。空 scope 的记忆是全局的，任何上下文都适用；
        查询不带 scope 时不过滤（用户没说是哪个店，就都给他看）。"""
        if not scope or not self.scope:
            return True
        return self.scope.strip().lower() == scope.strip().lower()

    @property
    def header_text(self) -> str:
        """检索时权重更高的部分：名字 + 描述 + 关键词。"""
        return f"{self.name} {self.description} {self.keywords}"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "category": self.category, "description": self.description,
                "keywords": self.keywords, "links": self.links, "created": self.created,
                "updated": self.updated, "body": self.body, "path": str(self.path),
                "valid_from": self.valid_from, "valid_until": self.valid_until,
                "scope": self.scope, "valid": self.is_valid_on(),
                "confidence": self.confidence, "evidence": self.evidence, "source": self.source}

    @property
    def uncertain(self) -> bool:
        return self.confidence < UNCERTAIN_BELOW

    def index_line(self) -> str:
        desc = self.description or (self.body.strip().splitlines() or [""])[0][:60]
        marks = []
        if self.scope:
            marks.append(self.scope)
        # 推断出来的记忆必须当着模型的面标出来。不标的话它会把"我猜你喜欢这样"
        # 和"你亲口说过要这样"一视同仁地执行，这正是错误信念固化的路径。
        if self.uncertain:
            marks.append("推断")
        # 有效期只在"不是无限期"时才标出来，否则每行都挂个尾巴，索引层白白变长
        if self.valid_until:
            marks.append(f"至{_norm_day(self.valid_until)}")
        elif self.valid_from:
            marks.append(f"自{_norm_day(self.valid_from)}起")
        tag = f"（{' · '.join(marks)}）" if marks else ""
        return f"- [{self.category}/{self.name}]{tag} {desc}"


# 用户亲口说的默认满信；反思推断的默认打折——它没被证实过，只是"看起来是这样"。
CONF_BY_SOURCE = {"user": 1.0, "manual": 1.0, "reflection": 0.5}
# 低于这个置信度的记忆，在给模型看的时候必须**明确标注是推断**，
# 不能和用户亲口说的规则混在一起——那正是错误信念固化的方式。
UNCERTAIN_BELOW = 0.75

# 反思推断的置信度**封顶在不确定线以下**。这是刻意的：证据再多也只说明"这类事发生过
# 很多次"，不说明"这是用户的偏好"——真实翻车案例里那条错误概括就有 3 条以上证据支撑。
# 想越过这条线只有一个途径：人确认。这样"未经确认的推断"永远带着标记，
# 不会混进用户亲口定下的规则里。
REFLECTION_MAX_CONFIDENCE = 0.70


def _clamp_conf(raw: Any, source: str) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return CONF_BY_SOURCE.get(source, 1.0)
    return max(0.0, min(1.0, v))


def _norm_day(value: str) -> str:
    """把 2026/8/1、2026-8-1 之类的手写日期归一成 2026-08-01，好做字典序比较。
    解析不出来就原样返回——宁可这条记忆当作长期有效，也不能因为日期格式让它凭空消失。"""
    s = (value or "").strip().replace("/", "-").replace(".", "-")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if not m:
        return s
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _load_file(path: Path) -> Optional[Entry]:
    try:
        meta, body = _parse_front(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return Entry(path, meta, body)


HISTORY_DIR = ".history"
# 待定区：反思新提出的洞察先落在这里，不进索引层、不进检索，等攒够证据或人确认才升级。
#
# 这就是综述里 dual-buffer consolidation 的落地——新记忆先"留观"再入库。动机是实测翻车：
# 反思把"用户几次在开发后要求发版"总结成了"用户习惯开发完就发版"，而真实规矩是
# **未经批准绝不发版**。这类错误概括统计上成立、证据门槛拦不住，只能靠"先别当真"来兜。
PENDING_DIR = ".pending"


def history_dir(category: str, name: str) -> Path:
    return mem_dir() / category / HISTORY_DIR / slugify(name)


def list_entries(*, include_expired: bool = False, scope: str = "",
                 on_day: str = "") -> List[Entry]:
    """当前有效的记忆，按更新时间倒序。直接扫目录——没有索引就没有索引漂移。

    默认**过滤掉已失效的**：一条"目标 ACoS 25%"如果三个月前就被改成 18% 了，
    它出现在检索结果里只会误导 agent。历史版本在 .history/ 里，
    用 `history()` 或 `include_expired=True` 才取得到。
    """
    root = mem_dir()
    if not root.exists():
        return []
    out: List[Entry] = []
    for cat_dir in sorted(root.iterdir()):
        # 跳过点开头的内部目录（.pending / .history）。不跳的话待定区会被当成一个"分类"，
        # **未经确认的推断直接泄漏进正常检索和索引层**——那正是待定区存在的意义所在。
        if not cat_dir.is_dir() or cat_dir.name.startswith("."):
            continue
        for f in sorted(cat_dir.glob("*.md")):
            e = _load_file(f)
            if not e:
                continue
            if not include_expired and not e.is_valid_on(on_day):
                continue
            if not e.matches_scope(scope):
                continue
            out.append(e)
    out.sort(key=lambda e: e.updated or "", reverse=True)
    return out


def history(name: str, category: str = "") -> List[Entry]:
    """一条记忆的历史版本（新→旧）。这是"什么时候改的、之前是什么"的唯一来源。"""
    cats = [category] if category else list(CATEGORIES)
    out: List[Entry] = []
    for cat in cats:
        d = history_dir(cat, name)
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md"), reverse=True):
            e = _load_file(f)
            if e:
                out.append(e)
    return out


def pending_dir() -> Path:
    return mem_dir() / PENDING_DIR


def list_pending() -> List[Entry]:
    d = pending_dir()
    if not d.exists():
        return []
    out = [e for e in (_load_file(f) for f in sorted(d.glob("*.md"))) if e]
    out.sort(key=lambda e: e.updated or "", reverse=True)
    return out


def get_pending(name: str) -> Optional[Entry]:
    p = pending_dir() / f"{slugify(name)}.md"
    return _load_file(p) if p.exists() else None


def add_pending(name: str, content: str, *, category: str = "", description: str = "",
                keywords: str = "", scope: str = "", evidence: str = "",
                confidence: Any = None, sightings: int = 1) -> Dict[str, Any]:
    """把一条新洞察放进待定区。同名再次出现则累加 sightings 并提高置信度——
    **反复被观察到**才是"这真是个规律"的信号，单次出现只是"发生过一次"。
    """
    name = (name or "").strip()
    if not name or not (content or "").strip():
        return {"ok": False, "message": "待定记忆需要 name 和 content。"}
    existing = get_pending(name)
    today = time.strftime("%Y-%m-%d")
    seen = sightings + (int(existing.keywords.split("sightings=")[-1].split(",")[0])
                        if existing and "sightings=" in existing.keywords else 0)
    conf = _clamp_conf(confidence, "reflection")
    if existing:
        conf = max(conf, existing.confidence + 0.1)
    # 封顶在不确定线以下：观察次数再多也只是"这个规律反复出现"，不等于"用户认可它"。
    # 不封的话 0.56→0.66→0.76 三次就悄悄越过 0.75，自动转正的推断会拿到"可信"身份，
    # 和用户亲口定的规则混为一谈——那正是这套待定机制要防的事。
    conf = min(conf, REFLECTION_MAX_CONFIDENCE)
    meta = {
        "name": name, "description": description or (existing.description if existing else ""),
        "category": category or (existing.category if existing else "domain"),
        # sightings 塞在 keywords 里而不是新开一个字段：待定区是内部暂存结构，
        # 升级时这些簿记信息会被丢掉，不值得污染正式 frontmatter 的字段表。
        "keywords": f"{keywords},sightings={seen}".strip(","),
        "created": (existing.created if existing else today), "updated": today,
        "scope": scope, "source": "reflection", "confidence": f"{conf:.2f}",
        "evidence": evidence or (existing.evidence if existing else ""),
    }
    p = pending_dir() / f"{slugify(name)}.md"
    _atomic_write(p, _render_front(meta, content))
    return {"ok": True, "name": name, "sightings": seen, "confidence": conf,
            "path": str(p), "message": f"待定记忆 [{name}]（第 {seen} 次观察到）"}


def promote_pending(name: str, *, confirmed_by_user: bool = False) -> Dict[str, Any]:
    """把待定记忆升级成正式记忆。

    `confirmed_by_user=True` 表示是人点头的——只有这条路径能让置信度越过"不确定线"，
    自动升级（攒够观察次数）仍然保留 reflection 身份和推断标记。
    """
    e = get_pending(name)
    if not e:
        return {"ok": False, "message": f"待定区里没有 {name!r}。"}
    res = apply("add", name=e.name, content=e.body, category=e.category,
                description=e.description,
                keywords=",".join(k for k in e.keywords.split(",") if not k.startswith("sightings=")),
                scope=e.scope, evidence=e.evidence,
                source="user" if confirmed_by_user else "reflection",
                confidence=1.0 if confirmed_by_user else e.confidence)
    if not res.get("ok"):
        # 撞上同名/查重时不能把待定记忆也丢了，否则这条洞察凭空消失
        return {"ok": False, "message": f"升级失败：{res.get('message')}（待定记忆已保留）"}
    try:
        Path(e.path).unlink()
    except OSError:
        pass
    how = "你确认" if confirmed_by_user else "累计证据"
    return {"ok": True, "name": e.name, "message": f"[{e.category}/{e.name}] 已转正（{how}）。"}


def reject_pending(name: str) -> Dict[str, Any]:
    e = get_pending(name)
    if not e:
        return {"ok": False, "message": f"待定区里没有 {name!r}。"}
    try:
        Path(e.path).unlink()
    except OSError as err:
        return {"ok": False, "message": f"删除失败：{err}"}
    return {"ok": True, "message": f"已丢弃待定记忆 [{e.name}]。"}


def _archive(entry: Entry, until_day: str) -> Optional[Path]:
    """把当前版本归档进 .history/，并盖上失效日期。

    归档而不是直接覆盖，是因为运营场景里"这个阈值以前是多少、什么时候改的"本身就是
    要回答的问题；覆盖式更新会把这段历史永久抹掉。
    """
    d = history_dir(entry.category, entry.name)
    d.mkdir(parents=True, exist_ok=True)
    # 秒级时间戳会在"同一秒内连续两次更新"时撞名，_atomic_write 直接覆盖 → 静默丢一版历史。
    # 归档的全部意义就是不丢历史，所以这里必须显式避让重名。
    base = time.strftime("%Y%m%d-%H%M%S")
    stamp, n = base, 1
    while (d / f"{stamp}.md").exists():
        n += 1
        stamp = f"{base}-{n:02d}"
    meta = {
        "name": entry.name, "description": entry.description, "category": entry.category,
        "keywords": entry.keywords, "links": entry.links, "created": entry.created,
        "updated": entry.updated, "scope": entry.scope,
        "valid_from": entry.valid_from or entry.created,
        "valid_until": until_day,
    }
    p = d / f"{stamp}.md"
    _atomic_write(p, _render_front(meta, entry.body))
    return p


def get(name: str, category: str = "") -> Optional[Entry]:
    """按名字取一条。不给分类就跨分类找——用户说"更新领星那条"时不会报分类。

    **刻意不过滤有效期**：按名字明确点名要某条记忆时，即便它已失效也该拿得到
    （否则 update 一条过期记忆会报"找不到"，只能新建，又碎片化了）。
    过滤只发生在检索路径上。
    """
    slug = slugify(name)
    cats = [category] if category else list(CATEGORIES)
    for cat in cats:
        p = mem_dir() / cat / f"{slug}.md"
        if p.exists():
            return _load_file(p)
    # 名字不完全一致时退化为不区分大小写的模糊匹配，模型偶尔会记错大小写或多写一个词
    low = slug.lower()
    for e in list_entries(include_expired=True):
        if slugify(e.name).lower() == low:
            return e
    return None


# ── 检索 ────────────────────────────────────────────────────────────────────
def search(query: str, limit: int = 8, *, scope: str = "",
           include_expired: bool = False, record: bool = True) -> List[Dict[str, Any]]:
    """在分类记忆里检索：词法初筛 → 语义重排（配了 dense 后端才生效）。

    词法这一路，名字/描述/关键词的权重是正文的 2 倍——记忆的"标题"是人为提炼过的，
    比正文里的零散措辞更能代表这条记忆讲什么。

    语义重排走 RRF，且**只对词法候选集重排**而不是全库算向量：全库现算向量在条目多了
    以后又慢又贵，而词法召回率在 bigram 下已经足够高，漏召的风险远小于成本收益。
    候选集取 limit 的若干倍，给语义留出把"词法排第 20 但语义最相关"那条捞上来的余地。
    """
    if not textseg.tokenize(query or ""):
        return []
    entries = list_entries(include_expired=include_expired, scope=scope)
    if not entries:
        return []

    # 词法召回：只有真正有重合的才进榜
    lex_scored: List[Tuple[float, int]] = []
    for idx, e in enumerate(entries):
        s = 2.0 * textseg.overlap_score(query, e.header_text) + textseg.overlap_score(query, e.body)
        if s > 0:
            lex_scored.append((s, idx))
    lex_scored.sort(key=lambda t: (-t[0], t[1]))
    lex_ranked = [idx for _, idx in lex_scored]
    lex_score = {idx: s for s, idx in lex_scored}

    # 向量召回走**全部**条目而不是词法候选集：词法零命中的口语化提问正是语义要救的场景。
    # 分类记忆是几百条量级、向量有缓存，全量算余弦就是几毫秒，不值得为此牺牲召回。
    from . import memory_vectors
    ranked = memory_vectors.hybrid_rank(
        query, entries, lambda e: f"{e.header_text} {e.body[:1500]}",
        limit=limit, lex_ranked=lex_ranked, budget=max(len(entries), memory_vectors.MAX_EMBED_PER_CALL))
    # 记一次召回：这是"用得多不多、最近用没用过"的唯一数据来源，
    # 没有它遗忘就只能按时间拍脑袋。放在返回前、失败不影响检索。
    #
    # `record=False` 给**系统内部发起**的检索用（写入时的关联推荐、评测跑分）。
    # 不区分的话：新建一条记忆会顺手给邻近记忆刷一次"被使用"，评测跑几轮能把全库
    # 刷成"热门"——遗忘打分的输入被它自己的副作用污染，就再也反映不了真实使用了。
    if record:
        from . import memory_decay
        memory_decay.record_hits(ranked)

    out = []
    for e in ranked:
        idx = entries.index(e)
        out.append({**e.to_dict(), "score": round(lex_score.get(idx, 0.0), 3)})
    return out


def find_similar(text: str, exclude: str = "", scope: str = "") -> Optional[Tuple[float, Entry]]:
    """查重：找出和给定文本讲同一件事的已有记忆。用于 add 前的合并优先判断。"""
    best: Optional[Tuple[float, Entry]] = None
    ex = slugify(exclude).lower() if exclude else ""
    # 查重只看**当前有效且同作用域**的：拿一条已失效的旧记忆去拦截新事实，
    # 等于永远记不下变更后的新值；不同店铺的同名打法也本来就该各记各的。
    for e in list_entries(scope=scope):
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

    # 按"用得多不多 / 最近用没用过 / 可不可信"排序并剔除冷门，而不是按时间。
    # 只按时间截断的话，一条你每周都在用的核心打法会被一条半年前的一次性结论挤掉——
    # 这正是记忆系统用到第三、四个月开始劣化的方式。
    from . import memory_decay
    ranked = memory_decay.rank(entries)
    active = [e for e, s in ranked if s["keep"]]
    archived_count = len(ranked) - len(active)

    by_cat: Dict[str, List[Entry]] = {}
    for e in active:
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
    hidden = truncated + archived_count
    if hidden:
        parts.append(f"（另有 {hidden} 条不常用的记忆未列出，用 memory_search 仍可检索到）")
    return "\n".join(parts)


# ── 写入：add / update / delete / noop ───────────────────────────────────────
def apply(operation: str, name: str = "", content: str = "", category: str = "",
          description: str = "", keywords: str = "", links: str = "",
          scope: str = "", valid_from: str = "", valid_until: str = "",
          supersede: bool = True, source: str = "", confidence: Any = None,
          evidence: str = "") -> Dict[str, Any]:
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
    # 查重是"读全库→判断→写文件"的读-改-写序列：CLI / serve(8765) / IvyeaOps 三端同时写时，
    # 两边会双双查重通过、各建一条近似重复的记忆。单文件的 os.replace 原子性救不了这个，
    # 必须让整个临界区互斥。
    from . import memory_lock
    with memory_lock.memory_write_lock():
        return _apply_locked(op, name, content, category, description, keywords, links,
                             scope, valid_from, valid_until, supersede, source, confidence,
                             evidence)


def _apply_locked(op: str, name: str, content: str, category: str, description: str,
                  keywords: str, links: str, scope: str, valid_from: str, valid_until: str,
                  supersede: bool, source: str, confidence: Any, evidence: str) -> Dict[str, Any]:

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
        return {"ok": False, "message": f"未知操作 {op!r}，可选：add / update / delete / noop"}

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
        sim = find_similar(f"{name} {description} {content}", scope=scope)
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
    src = (source or (existing.source if existing else "") or "user").strip()
    if confidence is not None:
        conf = _clamp_conf(confidence, src)
    elif existing and not source:
        conf = existing.confidence          # 普通更新不该悄悄改动置信度
    else:
        conf = CONF_BY_SOURCE.get(src, 1.0)
    # 用户亲自确认过的记忆被反思再次覆盖时，置信度**只升不降**：
    # 一条你亲口说过的规则，不该因为反思又推断了一遍就变成"推断"。
    if existing and existing.source in ("user", "manual") and src == "reflection":
        src, conf = existing.source, max(conf, existing.confidence)
    archived = None
    if op == "update" and existing and supersede and existing.body.strip() != content.strip():
        # 正文真的变了才归档：只改描述/关键词不该在历史里留一条内容相同的版本，
        # 否则 .history 会被无意义的版本塞满，真正的事实变更反而淹没在里面。
        archived = _archive(existing, until_day=today)

    meta = {
        "name": name,
        "description": (description or (existing.description if existing else "")).strip(),
        "category": cat,
        "keywords": (keywords or (existing.keywords if existing else "")).strip(),
        "links": (links or (existing.links if existing else "")).strip(),
        # created 保留原值：更新一条记忆不该抹掉它是什么时候第一次被记住的
        "created": (existing.created if existing else today) or today,
        "updated": today,
        "scope": (scope or (existing.scope if existing else "")).strip(),
        # 正文换了新事实 → valid_from 推到今天（旧事实的有效期已在归档里封口）；
        # 没换正文就保留原值，避免"改个错别字把生效日期也改了"。
        "valid_from": (_norm_day(valid_from) if valid_from else
                       (today if archived else (existing.valid_from if existing else ""))),
        "valid_until": _norm_day(valid_until) if valid_until else
                       (existing.valid_until if (existing and not archived) else ""),
        "source": src,
        "confidence": f"{conf:.2f}",
        "evidence": (evidence or (existing.evidence if existing else "")).strip(),
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
    msg = f"{verb}记忆 [{cat}/{name}]。"
    if archived:
        msg += f"旧版本已归档（{today} 起失效），用 ivyea memory history 可查。"
    if op == "add" and not meta["links"]:
        # 只在新建且没写链接时推荐，避免每次更新都刷一遍噪音
        sug = [s for s in link_suggestions(name, meta["description"], content)]
        if sug:
            msg += ("可能相关的已有记忆：" + "、".join(f"[[{s}]]" for s in sug)
                    + "。确实相关的话用 update 把它们写进 links，日后召回会一并带出。")
    return {"ok": True, "operation": op, "name": name, "category": cat, "path": str(path),
            "archived": str(archived) if archived else "", "message": msg}


# ── 关联图 ──────────────────────────────────────────────────────────────────
_LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")

# 联想跳数上限。1 跳即止是刻意的：人的联想也是"想起 A 顺带想起 B"，
# 不会一路串到第五层。放开跳数会让一次召回把半个记忆库拖进上下文，
# 和"省 token"的初衷正好相反。
MAX_LINK_HOPS = 1
# 每次召回最多带出多少条关联记忆，防止一条 hub 记忆（链接特别多的）淹没结果。
MAX_LINKED = 3


def parse_links(text: str) -> List[str]:
    """从 links 字段或正文里抽出 [[名字]]。正文里也认，是因为模型写正文时
    很自然会顺手打 [[xxx]]，强制它只写进 frontmatter 反而容易漏。"""
    return [m.strip() for m in _LINK_RE.findall(text or "") if m.strip()]


def entry_links(entry: "Entry") -> List[str]:
    seen, out = set(), []
    for name in parse_links(entry.links) + parse_links(entry.body):
        low = name.lower()
        if low not in seen:
            seen.add(low)
            out.append(name)
    return out


def backlinks(name: str) -> List["Entry"]:
    """谁链接到了这条。反向链接不落盘、每次现算——落盘就要维护一致性，
    而这套系统的原则是"文件即真相、不建会漂移的索引"。几百条记忆现算是毫秒级。"""
    target = slugify(name).lower()
    out = []
    for e in list_entries():
        if any(slugify(l).lower() == target for l in entry_links(e)):
            out.append(e)
    return out


def expand_linked(entries: List["Entry"], *, max_linked: int = MAX_LINKED,
                  hops: int = MAX_LINK_HOPS) -> List["Entry"]:
    """把召回结果的强关联记忆一并带出来——"想起一件事会带出相关的事"。

    只带**已在结果里的记忆所链接到的**，不做反向扩散：正向链接是作者明确写下的
    "这两件事相关"，反向则可能是任意一条记忆单方面提到了你，信噪比差得多。
    """
    if not entries:
        return entries
    known = {slugify(e.name).lower() for e in entries}
    out = list(entries)
    frontier = list(entries)
    added = 0
    for _ in range(max(0, hops)):
        nxt = []
        for e in frontier:
            for link in entry_links(e):
                if added >= max_linked:
                    return out
                low = slugify(link).lower()
                if low in known:
                    continue
                target = get(link)
                if not target or not target.is_valid_on():
                    continue
                known.add(low)
                out.append(target)
                nxt.append(target)
                added += 1
        frontier = nxt
        if not frontier:
            break
    return out


def link_suggestions(name: str, description: str, content: str, *, limit: int = 3) -> List[str]:
    """写入时推荐可能相关的已有记忆，让模型有机会显式建立关联。

    不自动建链：自动建的链会把"碰巧用词相似"当成"内容相关"，攒几个月就是一张
    到处都是边的图，联想反而变成噪音。推荐 + 由模型决定，信噪比高得多。
    """
    # record=False：写入时的关联推荐是系统行为，不该算作"这条记忆被用到了"
    hits = search(f"{name} {description} {content}"[:500], limit=limit + 1, record=False)
    return [h["name"] for h in hits if slugify(h["name"]).lower() != slugify(name).lower()][:limit]


def stats() -> Dict[str, Any]:
    entries = list_entries()
    by_cat: Dict[str, int] = {}
    for e in entries:
        by_cat[e.category] = by_cat.get(e.category, 0) + 1
    return {"total": len(entries), "by_category": by_cat, "dir": str(mem_dir()),
            "index_chars": len(index_digest())}
