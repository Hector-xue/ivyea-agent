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

from . import config

DB_PATH = config.IVYEA_DIR / "memory.db"
_FTS_OK: Optional[bool] = None


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
    conn.commit()
    return conn


def _index(conn: sqlite3.Connection, text: str, asin: str, ts: float) -> None:
    conn.execute("INSERT INTO search_fts (text, asin, ts) VALUES (?, ?, ?)", (text, asin, ts))


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


def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """全文检索。FTS5 的 unicode61 把整段中文当一个 token，无法子串匹配，
    故 FTS 命中为空时回退到 LIKE 子串检索（保证中文回忆可用）。"""
    conn = _conn()
    rows = []
    try:
        if _FTS_OK:
            rows = conn.execute("SELECT rowid, text, asin, ts FROM search_fts WHERE search_fts MATCH ? "
                                "ORDER BY rank LIMIT ?", (query, limit)).fetchall()
        if not rows:
            rows = _like_search(conn, query, limit)
    except Exception:
        rows = _like_search(conn, query, limit)
    conn.close()
    return [dict(r) for r in rows]


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


def stats() -> dict[str, Any]:
    conn = _conn()
    d = conn.execute("SELECT COUNT(*) c, SUM(decision='approve') a, SUM(decision='reject') r FROM decisions").fetchone()
    n = conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
    conn.close()
    return {"decisions": d["c"] or 0, "approved": d["a"] or 0, "rejected": d["r"] or 0,
            "runs": n, "fts": _FTS_OK, "db": str(DB_PATH)}


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


def load_memory_digest(limit: int = 3500) -> str:
    """启动注入用：全局 MEMORY.md 摘要 + 账户记忆索引，让 agent 开箱就知道记忆里有什么、不必每次
    靠回忆检索（曾出现"文件里明明有、recall 却说没有"）。超长则截断，其余仍可用「回忆记忆」检索。"""
    parts: list[str] = []
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
