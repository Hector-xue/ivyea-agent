"""运营记忆（Hermes 同款：SQLite + FTS5 + 自策展；本地自有，不依赖 GBrain/向量库）。

存 ~/.ivyea/memory.db：
- decisions：每个 ASIN+词+动作的人工裁决(approve/reject)与时间 → 支撑"尊重历史否决"
  和"5 天稳定期"。
- runs：每次巡检记录。
- search_fts：FTS5 全文检索(跨会话回忆)；FTS5 不可用时降级到普通表 + LIKE。

策展 markdown（MEMORY.md / account/<ASIN>.md）由 memory_md 提供。
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Optional

from . import config, memory_store, textseg

DB_PATH = config.IVYEA_DIR / "memory.db"
_FTS_OK: Optional[bool] = None
_TOK_OK: Optional[bool] = None


def _detect_fts(conn: sqlite3.Connection) -> bool:
    global _FTS_OK
    if _FTS_OK is None:
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(text, asin, ts UNINDEXED)")
            _FTS_OK = True
        except Exception:
            conn.execute("CREATE TABLE IF NOT EXISTS search_fts (text TEXT, asin TEXT, ts REAL)")
            _FTS_OK = False
    return _FTS_OK


def _detect_tok(conn: sqlite3.Connection) -> bool:
    """中文分词旁路索引：search_tok(tokens, src)，src 指向 search_fts 的 rowid。

    为什么做成**旁路**而不是改 search_fts 的表结构：search_fts 是 [对话]/[会话摘要]/
    [记忆]/[档] 这些行的**唯一存储**（decisions/runs 才有真表），动它的 schema 就得迁移
    真实数据，风险不对等。旁路表纯派生、可随时重建，加错了删掉就行。
    """
    global _TOK_OK
    if _TOK_OK is None:
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_tok USING fts5(tokens, src UNINDEXED)")
            _TOK_OK = bool(_FTS_OK)   # 主表退化成普通表时 rowid 语义不保证，索性一起降级
        except Exception:
            _TOK_OK = False
    return _TOK_OK


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    from . import memory_lock
    memory_lock.tune(conn)   # WAL + busy_timeout：多端同时用时不再 'database is locked'
    conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asin TEXT, term TEXT, kind TEXT, decision TEXT, ts REAL, note TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asin TEXT, ts REAL, negatives INTEGER, scale INTEGER, reduce INTEGER, note TEXT)""")
    _detect_fts(conn)
    _detect_tok(conn)
    conn.commit()
    return conn


def _index(conn: sqlite3.Connection, text: str, asin: str, ts: float) -> None:
    """写入检索行，并同步维护分词旁路索引。所有记忆写入（决策/巡检/记要点/对话/摘要/档）
    都收口在这里，所以分词索引只要挂这一个点就不会漏。"""
    cur = conn.execute("INSERT INTO search_fts (text, asin, ts) VALUES (?, ?, ?)", (text, asin, ts))
    if _TOK_OK:
        # 先清同 src 的旧行：**FTS5 删行后会复用 rowid**，sync_markdown_index 每次重灌 [档]
        # 都可能让新行拿到刚被删的 rowid。不清就会留下"src 相同、内容却是上一条记录"的陈旧
        # 分词行——它既不算孤儿（src 确实存在）也不算缺失，静默地把检索结果污染掉。
        conn.execute("DELETE FROM search_tok WHERE src = ?", (cur.lastrowid,))
        # asin 一并喂进分词索引：让「B08XYZ123 上次怎么处理的」这类查询命中该 ASIN 的所有行，
        # 哪怕正文里没再重复写一遍 ASIN。
        conn.execute("INSERT INTO search_tok (tokens, src) VALUES (?, ?)",
                     (textseg.index_text(f"{text} {asin}"), cur.lastrowid))


def record_decision(asin: str, term: str, kind: str, decision: str, note: str = "") -> None:
    """decision: approve | reject。"""
    ts = time.time()
    conn = _conn()
    conn.execute("INSERT INTO decisions (asin, term, kind, decision, ts, note) VALUES (?,?,?,?,?,?)",
                 (asin or "", term, kind, decision, ts, note))
    _index(conn, f"[决策] {decision} {kind} “{term}” {note}", asin or "", ts)
    conn.commit(); conn.close()


def record_run(asin: str, negatives: int = 0, scale: int = 0, reduce: int = 0, note: str = "") -> None:
    ts = time.time()
    conn = _conn()
    conn.execute("INSERT INTO runs (asin, ts, negatives, scale, reduce, note) VALUES (?,?,?,?,?,?)",
                 (asin or "", ts, negatives, scale, reduce, note))
    _index(conn, f"[巡检] {asin} 否词{negatives}/放量{scale}/降bid{reduce} {note}", asin or "", ts)
    conn.commit(); conn.close()


def was_rejected(asin: str, term: str, kind: str) -> bool:
    """该 ASIN+词+动作 最近一次人工裁决是否为 reject。"""
    conn = _conn()
    row = conn.execute(
        "SELECT decision FROM decisions WHERE asin=? AND term=? AND kind=? ORDER BY ts DESC LIMIT 1",
        (asin or "", term, kind)).fetchone()
    conn.close()
    return bool(row and row["decision"] == "reject")


def days_since_last_approve(asin: str, term: str, kinds: tuple = ("reduce_bid", "scale_up")) -> Optional[float]:
    """该 ASIN+词 最近一次被批准执行(调价类)距今天数；无则 None。"""
    conn = _conn()
    qs = ",".join("?" * len(kinds))
    row = conn.execute(
        f"SELECT ts FROM decisions WHERE asin=? AND term=? AND decision='approve' AND kind IN ({qs}) "
        "ORDER BY ts DESC LIMIT 1", (asin or "", term, *kinds)).fetchone()
    conn.close()
    return (time.time() - row["ts"]) / 86400.0 if row else None


def recent_runs(asin: str = "", limit: int = 5) -> list[dict[str, Any]]:
    conn = _conn()
    if asin:
        rows = conn.execute("SELECT * FROM runs WHERE asin=? ORDER BY ts DESC LIMIT ?", (asin, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _like_search(conn, query: str, limit: int):
    return conn.execute("SELECT rowid, text, asin, ts FROM search_fts WHERE text LIKE ? "
                        "ORDER BY ts DESC LIMIT ?", (f"%{query}%", limit)).fetchall()


def _tok_search(conn, query: str, limit: int):
    """走分词旁路索引 + FTS5 内建 bm25 排序。

    bm25() 返回的是**负分**（越负越相关），所以 ORDER BY score 升序就是相关度降序——
    别看到负数就以为排反了。
    """
    mq = textseg.match_query(query)
    if not mq:
        return []
    return conn.execute(
        "SELECT f.rowid AS rowid, f.text AS text, f.asin AS asin, f.ts AS ts, "
        "       bm25(search_tok) AS score "
        "FROM search_tok JOIN search_fts f ON f.rowid = search_tok.src "
        "WHERE search_tok MATCH ? ORDER BY score LIMIT ?", (mq, limit)).fetchall()


def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """全文检索。三级降级：分词索引(bm25) → 原始 FTS → LIKE 子串。

    第一级是主力：FTS5 自带的 unicode61 把整段中文当成一个 token，中文只有整段一模一样
    才命中，等于中文检索失效；textseg 预先把中文切成 bigram 存进 search_tok，才让
    "广告花钱太狠" 能召回 "广告花费太高"。后两级保留是为了老库（旁路索引还没建）
    和 FTS5 不可用的环境仍能用。
    """
    # 空/纯标点查询直接返回空：否则会一路降级到 `LIKE '%%'`，把**整个记忆库**当成命中结果
    # 灌回给模型。用分词结果判空而不是 strip()，因为"，。！"这种也应当算无检索内容。
    if not textseg.tokenize(query or ""):
        return []
    # 候选池取得比 limit 大：语义重排要有"把词法排第 20 的那条捞上来"的余地，
    # 池子等于 limit 的话重排就只能在已选中的几条里换顺序，等于没重排。
    # 没有 dense 后端时 hybrid_rank 原样截前 limit 条，结果与扩池前**逐条相同**（顺序未变）。
    pool = max(limit * 4, 24)
    conn = _conn()
    rows = []
    try:
        if _TOK_OK:
            rows = _tok_search(conn, query, pool)
        if not rows and _FTS_OK:
            rows = conn.execute("SELECT rowid, text, asin, ts FROM search_fts WHERE search_fts MATCH ? "
                                "ORDER BY rank LIMIT ?", (query, pool)).fetchall()
        if not rows:
            rows = _like_search(conn, query, pool)
    except Exception:
        try:
            rows = _like_search(conn, query, pool)
        except Exception:
            rows = []
    conn.close()
    # score 只是内部排序用，不进对外结果（调用方按 text/asin/ts 消费，多一个键会污染 JSON 输出）
    out = [{k: r[k] for k in ("rowid", "text", "asin", "ts")} for r in rows]
    # 语义重排：没配 dense 后端时 hybrid_rank 原样返回，行为与纯词法完全一致
    try:
        from . import memory_vectors
        return memory_vectors.hybrid_rank(query, out, lambda r: r["text"], limit=limit)
    except Exception:   # noqa: BLE001 —— 语义是增益，挂了也必须还给调用方词法结果
        return out[:limit]


def index_rows(limit: int = 5000) -> list[dict[str, Any]]:
    """Return memory search rows for the persistent retrieval index."""
    lim = max(1, min(int(limit or 5000), 50000))
    conn = _conn()
    rows = conn.execute(
        "SELECT rowid, text, asin, ts FROM search_fts ORDER BY ts DESC LIMIT ?",
        (lim,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def rebuild_token_index(force: bool = False) -> dict[str, Any]:
    """把分词旁路索引对齐到 search_fts（幂等、增量）。

    三种漂移都要修：
    1. **老库升级**：升级前写入的行没有分词索引 → 补齐；
    2. **源行被删**：sync_markdown_index 会重写 [档] 行 → 清掉指向已消失 rowid 的孤儿；
    3. **强制重建**：分词规则改版后 token 语义变了 → force=True 全量重来。

    刻意做成增量而不是每次全量：启动路径上会调它，语料涨到几万条时全量重切会拖慢冷启动，
    而增量的代价只和"这次新增/删除了多少行"成正比。
    """
    conn = _conn()
    if not _TOK_OK:
        conn.close()
        return {"ok": False, "reason": "FTS5 不可用，分词索引已降级"}
    try:
        if force:
            conn.execute("DELETE FROM search_tok")
        removed = conn.execute(
            "DELETE FROM search_tok WHERE src NOT IN (SELECT rowid FROM search_fts)").rowcount
        # 同 src 的重复行只留最新一条（rowid 最大）。老库在修复 rowid 复用问题之前写入的
        # 陈旧分词行就是这么攒下来的，光靠孤儿清理认不出来。
        removed += max(0, conn.execute(
            "DELETE FROM search_tok WHERE rowid NOT IN "
            "(SELECT MAX(rowid) FROM search_tok GROUP BY src)").rowcount)
        rows = conn.execute(
            "SELECT rowid, text, asin FROM search_fts "
            "WHERE rowid NOT IN (SELECT src FROM search_tok)").fetchall()
        for r in rows:
            conn.execute("INSERT INTO search_tok (tokens, src) VALUES (?, ?)",
                         (textseg.index_text(f"{r['text']} {r['asin'] or ''}"), r["rowid"]))
        conn.commit()
        return {"ok": True, "added": len(rows), "removed": max(0, removed)}
    except Exception as e:  # noqa: BLE001
        from . import log
        log.dbg("memory.rebuild_token_index", f"重建分词索引失败: {e!r}")
        return {"ok": False, "reason": str(e)}
    finally:
        conn.close()


# 情景记忆的行前缀。[档] 不算——它是策展 markdown 的派生副本，不是新发生的经历，
# 拿它去反思等于把已经沉淀过的结论再嚼一遍。
EPISODE_PREFIXES = ("[对话:", "[会话摘要]", "[记忆]", "[决策]", "[巡检]")


def episodes_since(ts: float = 0.0, limit: int = 200) -> list[dict[str, Any]]:
    """取某时刻之后的情景记忆，按时间正序（反思要按事情发生顺序读才看得出规律）。"""
    conn = _conn()
    like = " OR ".join("text LIKE ?" for _ in EPISODE_PREFIXES)
    params: list[Any] = [f"{p}%" for p in EPISODE_PREFIXES]
    rows = conn.execute(
        f"SELECT rowid, text, asin, ts FROM search_fts WHERE ts > ? AND ({like}) "
        "ORDER BY ts ASC LIMIT ?", [float(ts or 0.0), *params, int(limit)]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats() -> dict[str, Any]:
    conn = _conn()
    d = conn.execute("SELECT COUNT(*) c, SUM(decision='approve') a, SUM(decision='reject') r FROM decisions").fetchone()
    n = conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
    rows = conn.execute("SELECT COUNT(*) c FROM search_fts").fetchone()["c"]
    toks = conn.execute("SELECT COUNT(*) c FROM search_tok").fetchone()["c"] if _TOK_OK else 0
    conn.close()
    return {"decisions": d["c"] or 0, "approved": d["a"] or 0, "rejected": d["r"] or 0,
            "runs": n, "fts": _FTS_OK, "db": str(DB_PATH),
            # indexed/tokenized 不等时说明分词索引漂移了，rebuild_token_index() 可修
            "indexed": rows, "tokenized": toks, "segmented_search": bool(_TOK_OK)}


def note_path(asin: str = ""):
    if asin:
        d = config.IVYEA_DIR / "account"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{asin}.md"
    return config.IVYEA_DIR / "MEMORY.md"


def read_note(asin: str = "") -> str:
    p = note_path(asin)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def remember(text: str, asin: str = "") -> str:
    """把一条要点追加到策展 markdown（MEMORY.md 或 account/<asin>.md）并入检索。"""
    text = (text or "").strip()
    if not text:
        return "（空，未记）"
    p = note_path(asin)
    ts = time.strftime("%Y-%m-%d %H:%M")
    head = "" if p.exists() else (f"# {asin} 运营记忆\n\n" if asin else "# Ivyea Agent 记忆\n\n")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"{head}- [{ts}] {text}\n")
    conn = _conn()
    _index(conn, f"[记忆] {asin} {text}", asin or "", time.time())
    conn.commit(); conn.close()
    return f"已记到 {p.name}"


# ── 持久指令（CLAUDE.md/AGENTS.md 同款）────────────────────────────────────────
def instruction_paths(cwd: str = "") -> list:
    """全局画像/账户指令 + 项目级指令（优先级：全局 → 项目）。"""
    from pathlib import Path
    paths = [config.IVYEA_DIR / "USER.md", config.IVYEA_DIR / "AGENTS.md"]
    if cwd:
        paths.append(Path(cwd) / "AGENTS.md")
    return paths


def sync_markdown_index() -> None:
    """把策展 markdown（MEMORY.md + account/*.md）同步进 FTS 索引（幂等）。修复漂移：用户直接手改
    MEMORY.md（不走 remember 工具）或重装后 memory.db 丢失而 markdown 仍在时，内容进了文件却没进
    索引→FTS/语义召回抓瞎。以 [档] 前缀标记文件来源行，重建时只清这些行、不动 decision/run/turn/
    [记忆] 等其它行。每进程调一次即可（文件小，成本低）。"""
    import re
    try:
        paths = [note_path("")]
        acc = config.IVYEA_DIR / "account"
        if acc.exists():
            paths.extend(sorted(acc.glob("*.md")))
        conn = _conn()
        conn.execute("DELETE FROM search_fts WHERE text LIKE '[档]%'")
        for p in paths:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            asin = p.stem if p.parent.name == "account" else ""
            for block in (b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()):
                _index(conn, (f"[档] {asin} {block}")[:4000], asin, time.time())
        conn.commit()
        conn.close()
    except Exception:
        pass
    # 上面刚删掉一批 [档] 源行，会在分词旁路索引里留下孤儿；顺手把索引对齐。
    # 同时这也是老库（升级前的行没有分词索引）补齐的入口——它在 CLI 启动路径上被调用。
    rebuild_token_index()


def load_memory_digest(limit: int = 3500, *, scope: str = "") -> str:
    """启动注入用：分类记忆**索引层** + 全局 MEMORY.md 摘要 + 账户记忆索引，
    让 agent 开箱就知道记忆里有什么、不必每次靠回忆检索
    （曾出现"文件里明明有、recall 却说没有"）。超长则截断，其余仍可用「回忆记忆」检索。

    索引层排在最前：它是每条记忆一行的目录，模型据此判断该取哪条正文，
    比把正文全塞进来省得多——这是整套记忆方案省 token 的关键。
    """
    parts: list[str] = []
    try:
        from . import memory_store
        index = memory_store.index_digest(scope=scope)
        if index:
            parts.append("[分类记忆索引]（需要正文时用 memory_read/memory_search 取）\n" + index)
    except Exception:
        pass
    try:
        p = note_path("")
        if p.exists():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                if len(text) > limit:
                    text = text[:limit].rstrip() + "\n…（记忆较长，其余用「回忆记忆」检索）"
                parts.append(text)
    except Exception:
        pass
    # 账户记忆索引：列出有哪些 account/<asin>.md，agent 知道其存在、可按需「回忆记忆」或读文件
    try:
        acc = config.IVYEA_DIR / "account"
        if acc.exists():
            asins = sorted(f.stem for f in acc.glob("*.md"))
            if asins:
                parts.append("已有账户记忆（account/<asin>.md，需要时用「回忆记忆」或读文件查看）："
                             + ", ".join(asins))
    except Exception:
        pass
    return "\n\n".join(parts).strip()


def load_instructions(cwd: str = "", limit: int = 6000) -> str:
    """汇总 USER.md(画像) + AGENTS.md(账户/项目打法)，启动注入 system。"""
    parts = []
    for p in instruction_paths(cwd):
        try:
            if p.exists():
                t = p.read_text(encoding="utf-8").strip()
                if t:
                    parts.append(f"# {p.name}\n{t}")
        except Exception as e:
            from . import log
            log.dbg("memory.instructions", f"读取 {p} 失败: {e!r}")
    return "\n\n".join(parts)[:limit]


_AGENTS_TEMPLATE = """# 账户运营指令（AGENTS.md）

> Ivyea Agent 每次启动会读取本文件并注入上下文。写你希望它长期遵守的打法与边界。

## 店铺与目标
- 主营类目 / 站点：
- 目标 ACoS（或留空让它按毛利率推）：
- 保护词（绝不否定）：品牌词、核心品类词…

## 打法偏好
- 否词：≥15 点击 0 单才否（保守）
- 调 bid：单步 ≤15%，冷却 7 天
- 旺季 / 大促节奏：

## 边界（红线）
- 不投 SBV / 不走 Vine / 不操控评论
- 任何写操作必须人工逐条确认
"""


def init_agents(path: str) -> tuple:
    """生成 AGENTS.md 模板。返回 (是否新建, 路径)。已存在则不覆盖。"""
    from pathlib import Path
    p = Path(path)
    if p.exists():
        return False, str(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_AGENTS_TEMPLATE, encoding="utf-8")
    return True, str(p)


# ── 会话转录回忆 + 摘要入库 ──────────────────────────────────────────────────
def index_turn(role: str, text: str, session_id: str = "") -> None:
    """把一轮对话入 FTS，支撑跨会话「上次聊到的那个…」回忆。"""
    text = (text or "").strip()
    if not text:
        return
    conn = _conn()
    _index(conn, f"[对话:{role}] {text[:1000]}", "", time.time())
    conn.commit(); conn.close()


_NUDGE_KEYS = ("否词", "否决", "降bid", "加bid", "调价", "收割", "加预算", "放量")


def nudge_hint(assistant_text: str) -> str:
    """自策展提示：回复涉及打法/决策且未在记要点时，提醒可长期沉淀。"""
    t = assistant_text or ""
    if any(k in t for k in _NUDGE_KEYS) and "记住" not in t:
        return "想让我长期记住这条打法/否决？说一句「记住…」即可，下次自动遵守。"
    return ""


def remember_summary(text: str, session_id: str = "") -> None:
    """把上下文压缩出的会话摘要入库（长期可召回）。"""
    text = (text or "").strip()
    if not text:
        return
    conn = _conn()
    _index(conn, f"[会话摘要] {text[:2000]}", "", time.time())
    conn.commit(); conn.close()


def annotate(actions: list, asin: str, stability_days: int = 5) -> list:
    """记忆护栏：把"历史已否决/稳定期内"的动作标记为 blocked（叠加在硬护栏之上）。"""
    if not asin:
        return actions
    for a in actions:
        if a.blocked:
            continue
        if was_rejected(asin, a.search_term, a.kind):
            a.blocked, a.block_reason = True, "记忆：上次人工已否决，不再自动建议（如需可手动执行）"
        elif a.kind in ("reduce_bid", "scale_up"):
            d = days_since_last_approve(asin, a.search_term)
            if d is not None and d < stability_days:
                a.blocked, a.block_reason = True, f"记忆：{stability_days} 天稳定期内（{d:.1f} 天前刚调过），不重复调"
    return actions

# ── 每轮自动召回（runtime-driven）────────────────────────────────────────────
#
# 此前记忆是 **model-driven** 的：模型得自己想起来去调 memory_search。
# 于是"想不起来"成了最常见的失败模式——而这正是 ChatGPT / Hermes 用起来"记性好"
# 的原因：它们由**运行时**在每轮开口前先查一遍，模型根本没有"忘了查"的机会。
#
# 注入形态刻意选择**后缀**（拼在用户这句话尾巴上），和 `[Ivyea 本地知识检索]`
# 并排 —— serve 每轮自动注入知识证据的那套管道已经在生产上跑了很久、验证过了，
# 记忆只是换一个检索源挂进去，没必要另起一套独立消息 + 门禁的机制。

RECALL_MARKER = "[Ivyea 记忆召回]"

# 召回块整体上限。它每轮都进上下文，不限长就会和知识证据一起把窗口吃光。
RECALL_MAX_CHARS = 1200

# 没有语义信号的话：寒暄、应答、纯标点、斜杠命令。
# 锚定 + 只允许尾随标点，所以"行不行"不会被"行"命中、"好的方案是什么"不会被"好的"命中。
# 中文这半边是 ivyea 自己加的：Hermes 那份只有英文，直接抄过来的话
# "好的""继续""收到"全都漏网，而中文对话里它们占了相当大比例。
_TRIVIAL_RE = re.compile(
    r"^(?:"
    r"yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"hi|hey|hello|yo|sup|continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k"
    r"|好|好的|好吧|行|行吧|可以|嗯|嗯嗯|哦|噢|是|对|对的|没问题|收到|知道了|明白|懂了"
    r"|继续|接着|然后呢|下一步|开始|你好|在吗|在么|嗨|谢谢|谢了|多谢|辛苦了|麻烦了"
    r"|不用|不用了|算了|停|停下|等一下|稍等"
    r")"
    r"[\s!?.:;,、。！？；：·…~\u2018\u2019\u201c\u201d\u2014\u2013()\[\]{}<>*&^%$#@+=`\u00a0'\"]*$",
    re.IGNORECASE,
)


def is_trivial_prompt(text: str) -> bool:
    """这句话值不值得为它跑一次检索。

    空输入、斜杠命令、纯寒暄/应答一律不值得：检索要花时间和 token，而"好的"这种话里
    没有任何可供检索的信号；更糟的是拿它去查，召回的会是上一个话题的残留，
    把一句本该一行带过的回答带偏。
    """
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("/"):
        return True
    return bool(_TRIVIAL_RE.match(t))


def recall_core(query: str, *, limit: int = 4, episodes: int = 6, scope: str = "",
                record: bool = False) -> dict[str, Any]:
    """记忆检索的**唯一**核心。`recall` 工具和每轮自动召回都走这里。

    两条召回路径各写一份的话，早晚会漂移——而漂移的那条不会有人发现，直到某天
    发现"工具查得到、自动召回查不到"。

    `record=False` 是默认值，且自动召回**必须**用它：自动召回每轮都跑，
    把它计入"这条记忆被用到了"会让遗忘打分彻底失真（冷门记忆全变成热门）。
    """
    query = (query or "").strip()
    if not query:
        return {"curated": [], "episodes": []}
    try:
        curated = memory_store.search(query, limit=limit, scope=scope, record=record)
    except Exception:  # noqa: BLE001
        curated = []
    try:
        eps = search(query, limit=episodes)
    except Exception:  # noqa: BLE001
        eps = []
    return {"curated": curated, "episodes": eps}


def already_recalled(messages: list[dict[str, Any]]) -> set[str]:
    """本会话此前已经注入过哪些记忆条目。

    **为什么必须去重**：召回块是拼在用户消息里、跟着一起落盘的（resume 要靠它复原
    现场）。不去重的话 30 轮对话就堆 30 份召回，同一条记忆被反复注入——token 白烧，
    更糟的是**已经被 delete / 被推翻的记忆仍然留在历史里继续影响模型**。

    直接从对话里反查，不额外维护状态：这样续接会话、换进程、compact 之后都自动正确。
    """
    seen: set[str] = set()
    for msg in messages or []:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or RECALL_MARKER not in content:
            continue
        for m in re.finditer(r"·\s*\[([^\]/]+)/([^\]]+)\]", content):
            seen.add(f"{m.group(1).strip()}/{m.group(2).strip()}")
    return seen


def auto_recall_text(query: str, *, exclude=None, scope: str = "",
                     limit: int = 4):
    """本轮要注入的召回正文 + 命中的条目名。没有新东西可注入时返回 ("", [])。

    只回**记忆**，不带知识卡：serve 每轮已经单独注入过知识证据了，再带一份是重复；
    而且知识卡的正文必须经 knowledge_search 走一遍才会登记引证键，
    这里连指针都不给，就彻底不存在"拿没登记的 [K?] 去标注结论"的风险。
    """
    exclude = exclude or set()
    # 候选窗口就是 limit，**不为去重扩窗**。
    #
    # 扩窗（limit + len(exclude)）看着更"充分利用配额"，实测是坏的：第二轮问同一件
    # 事时，前 4 条都被去重挡掉，于是拿第 5~7 条来补位 —— 那些是勉强过了词法地板的
    # 边缘条目，和这一轮基本无关。结果就是"聊得越久，注进去的记忆越离题"。
    # 宁可这一轮什么都不注（模型手里还有索引层和 memory_search），也不注垃圾。
    hit = recall_core(query, limit=limit, scope=scope, record=False)
    lines: list[str] = []
    names: list[str] = []
    for h in hit["curated"]:
        # **自动召回必须比 recall 工具苛刻**：只收有真实词法重合的条目。
        #
        # 语义那一路是**没有相似度地板**的（刻意的：实测 bge 正确匹配只有 0.41~0.55、
        # 错配 0.29~0.55，按直觉设的阈值会把正确结果静默杀光）。于是它对任何查询
        # 都会按余弦返回前 N 条 —— 用户问一句跟记忆毫无关系的话，照样能排出"最相似
        # 的四条"。对 recall 工具这没问题（人主动要求回忆，给个最佳猜测是对的），
        # 但自动召回是**每轮无条件注入**：没有地板就等于每轮往上下文里塞四条随机记忆，
        # 把一句本该一行带过的回答带偏。
        #
        # 词法重合是这里唯一可信的信号。代价是纯语义命中（口语化提问）进不了自动召回 ——
        # 那类查询交给 memory_search 工具，以及后面要上的 LLM 查询改写
        # （它把"这个再改改"改写成带实词的问句，正好把词法信号还回来）。
        if float(h.get("score") or 0.0) <= 0.0:
            continue
        key = f"{h['category']}/{h['name']}"
        if key in exclude:
            continue                      # 这条本会话早注入过了，别再占一次位置
        desc = (h.get("description") or (h.get("body") or "")[:60]).replace("\n", " ")
        # 置信度低的要标出来：反思推断出来的东西和用户亲口说的不该被同等对待
        mark = " ⚠推断" if float(h.get("confidence", 1.0)) < memory_store.UNCERTAIN_BELOW else ""
        lines.append(f"  · [{key}]{mark} {desc}")
        names.append(key)
        if len(names) >= limit:
            break
    body = "\n".join(lines)
    if len(body) > RECALL_MAX_CHARS:
        body = body[:RECALL_MAX_CHARS].rstrip() + "\n  …（更多用 memory_search 查）"
    if not body:
        return "", []
    return body, names


def recall_block(body: str) -> str:
    """把召回正文包成注入块。

    那句"不是用户本轮输入"是必须的：不写的话模型会把召回内容当成用户刚说的话，
    然后一本正经地回应记忆里的旧话题。
    """
    return (f"\n\n{RECALL_MARKER}\n{body}\n"
            "（以上是你的长期记忆，不是用户本轮输入；相关就用，无关就忽略，不要复述。"
            "需要全文用 memory_read。）")
