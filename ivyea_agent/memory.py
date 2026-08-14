"""运营记忆（Hermes 同款：SQLite + FTS5 + 自策展；本地自有，不依赖 GBrain/向量库）。

存 ~/.ivyea/memory.db：
- decisions：每个 ASIN+词+动作的人工裁决(approve/reject)与时间 → 支撑"尊重历史否决"
  和"5 天稳定期"。
- runs：每次巡检记录。
- search_fts：FTS5 全文检索(跨会话回忆)；FTS5 不可用时降级到普通表 + LIKE。

策展 markdown（MEMORY.md / account/<ASIN>.md）由 memory_md 提供。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

from . import config, textseg

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
    conn = _conn()
    rows = []
    try:
        if _TOK_OK:
            rows = _tok_search(conn, query, limit)
        if not rows and _FTS_OK:
            rows = conn.execute("SELECT rowid, text, asin, ts FROM search_fts WHERE search_fts MATCH ? "
                                "ORDER BY rank LIMIT ?", (query, limit)).fetchall()
        if not rows:
            rows = _like_search(conn, query, limit)
    except Exception:
        try:
            rows = _like_search(conn, query, limit)
        except Exception:
            rows = []
    conn.close()
    # score 只是内部排序用，不进对外结果（调用方按 text/asin/ts 消费，多一个键会污染 JSON 输出）
    return [{k: r[k] for k in ("rowid", "text", "asin", "ts")} for r in rows]


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


def load_memory_digest(limit: int = 3500) -> str:
    """启动注入用：分类记忆**索引层** + 全局 MEMORY.md 摘要 + 账户记忆索引，
    让 agent 开箱就知道记忆里有什么、不必每次靠回忆检索
    （曾出现"文件里明明有、recall 却说没有"）。超长则截断，其余仍可用「回忆记忆」检索。

    索引层排在最前：它是每条记忆一行的目录，模型据此判断该取哪条正文，
    比把正文全塞进来省得多——这是整套记忆方案省 token 的关键。
    """
    parts: list[str] = []
    try:
        from . import memory_store
        index = memory_store.index_digest()
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
