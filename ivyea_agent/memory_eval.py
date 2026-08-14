"""记忆检索评测：把"召回质量有没有变好"变成数字。

**为什么必须有这个模块**：没有评测，所有检索改动都只能靠感觉。这套系统已经栽过两次——
按直觉设的相似度阈值 0.55 把所有语义结果静默杀光了，把语义做成"重排词法候选"让口语化
查询 0/3 命中。两次都是**跑真实数据才发现**的，不是想出来的。评测集就是把这种发现
自动化、并且防止改回去。

指标用信息检索的标准三件套：
- `recall@k`：正确答案出现在前 k 条里的比例。**k=5 最能反映实际体验**——agent 会把
  前几条都读一遍，不是只看第一条。
- `mrr`：正确答案排名的倒数均值。同样是命中，排第 1 比排第 5 有用得多，recall@k
  看不出这个差别。
- `top1`：最严格的口径，用来看排序是不是真的准。

评测集来源有两条：手写（准，但攒得慢）和 `generate` 从现有记忆用 LLM 反向造问题
（快，覆盖全，但问题风格偏书面）。两条都支持，混着用最实际。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

DEFAULT_K = (1, 3, 5)


def eval_dir() -> Path:
    return config.IVYEA_DIR / "memory_eval"


def dataset_path(name: str = "default") -> Path:
    return eval_dir() / f"{name}.json"


def load_dataset(name: str = "default") -> List[Dict[str, Any]]:
    """评测集：[{"query": ..., "expect": ["记忆名", ...], "note": ...}]。

    `expect` 写记忆名而不是正文片段：正文会随着 update/反思不断改写，
    拿正文做断言等于每改一次记忆就要改一次评测集，没人会维护。
    """
    p = dataset_path(name)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if isinstance(row, dict) and row.get("query") and row.get("expect"):
            expect = row["expect"]
            out.append({"query": str(row["query"]),
                        "expect": [str(e) for e in (expect if isinstance(expect, list) else [expect])],
                        "note": str(row.get("note") or "")})
    return out


def save_dataset(cases: List[Dict[str, Any]], name: str = "default") -> Path:
    p = dataset_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _rank_of(hits: List[Dict[str, Any]], expect: List[str]) -> Optional[int]:
    """正确答案在结果里的名次（1 起）；没命中返回 None。任一 expect 命中即算命中。"""
    wanted = {e.strip().lower() for e in expect}
    for i, h in enumerate(hits, start=1):
        if str(h.get("name") or "").strip().lower() in wanted:
            return i
    return None


def _metrics(ranks: List[Optional[int]], ks=DEFAULT_K) -> Dict[str, Any]:
    n = len(ranks) or 1
    out: Dict[str, Any] = {"cases": len(ranks)}
    for k in ks:
        out[f"recall@{k}"] = round(sum(1 for r in ranks if r is not None and r <= k) / n, 4)
    out["mrr"] = round(sum((1.0 / r) for r in ranks if r) / n, 4)
    out["missed"] = sum(1 for r in ranks if r is None)
    return out


def run(name: str = "default", *, limit: int = 5, semantic: Optional[bool] = None) -> Dict[str, Any]:
    """跑一遍评测。`semantic=False` 强制纯词法，用来和混合模式对比。"""
    from . import memory_store, memory_vectors

    cases = load_dataset(name)
    if not cases:
        return {"ok": False, "message": f"评测集 {name} 为空。先用 ivyea memory eval --generate 生成。"}

    def _run_all() -> List[Optional[int]]:
        return [_rank_of(memory_store.search(c["query"], limit=limit), c["expect"]) for c in cases]

    if semantic is False:
        with memory_vectors.lexical_only():
            ranks = _run_all()
        active = False
    else:
        ranks = _run_all()
        # 报**实际生效**的语义状态，不是请求值：没配后端时若打印"语义 on"，
        # 读数字的人会把纯词法的成绩误当成语义的功劳，比没有指标更糟。
        active = memory_vectors.backend_key()[2]

    details = []
    for c, r in zip(cases, ranks):
        details.append({"query": c["query"], "expect": c["expect"], "rank": r})
    return {"ok": True, "dataset": name, "semantic": active, "corpus": _corpus_size(),
            **_metrics(ranks), "details": details}


def _corpus_size() -> int:
    from . import memory_store
    return len(memory_store.list_entries())


def compare(name: str = "default", *, limit: int = 5) -> Dict[str, Any]:
    """同一份数据上跑纯词法和混合两遍，给出增量。这是判断"语义值不值得开"的唯一依据。"""
    from . import memory_vectors

    lex = run(name, limit=limit, semantic=False)
    if not lex.get("ok"):
        return lex
    hyb = run(name, limit=limit, semantic=True)
    _, _, dense = memory_vectors.backend_key()
    delta = {}
    for key in [f"recall@{k}" for k in DEFAULT_K] + ["mrr"]:
        delta[key] = round(hyb.get(key, 0) - lex.get(key, 0), 4)
    return {"ok": True, "dataset": name, "semantic_available": dense,
            "lexical": {k: v for k, v in lex.items() if k != "details"},
            "hybrid": {k: v for k, v in hyb.items() if k != "details"},
            "delta": delta,
            # 逐条列出"语义救回来的"和"语义弄坏的"，光看总分看不出是哪类查询在变化
            "rescued": [d["query"] for d, l in zip(hyb["details"], lex["details"])
                        if d["rank"] and not l["rank"]],
            "regressed": [d["query"] for d, l in zip(hyb["details"], lex["details"])
                          if l["rank"] and not d["rank"]]}


_GEN_SYS = """你在为一个记忆检索系统造评测集。用户会给你一条记忆的名字、描述和正文。
你要写出 3 个**用户可能真的会说出口的问题**，这些问题问的就是这条记忆里的事。

关键要求：
- 用**口语**，不要照抄记忆里的措辞。评测的价值就在于检验"换个说法还能不能找到"，
  照抄原词的问题测不出任何东西。
- 三个问题的说法要互相不同：一个偏日常口语、一个偏专业术语、一个偏场景描述。
- 不要问记忆里没有的信息。

只输出 JSON：{"queries": ["问题1", "问题2", "问题3"]}"""


def generate(provider, *, name: str = "default", per_entry: int = 3,
             merge: bool = True) -> Dict[str, Any]:
    """用 LLM 从现有分类记忆反向生成评测问题。

    这是攒评测集最实际的办法：手写几十条问答对没人有耐心，而记忆本身就是现成的答案，
    反过来造问题既快又能覆盖全库。缺点是问题风格偏书面，所以 prompt 里特意要求口语化，
    并且保留手写用例（merge=True）——手写的那些通常来自真实翻车案例，比生成的值钱。
    """
    from . import memory_store

    entries = memory_store.list_entries()
    if not entries:
        return {"ok": False, "message": "还没有分类记忆，无法生成评测集。"}

    existing = load_dataset(name) if merge else []
    # 已经有用例的记忆不重复生成，避免每次 --generate 都翻倍
    covered = {e.lower() for c in existing for e in c["expect"]}

    cases: List[Dict[str, Any]] = list(existing)
    made = 0
    failed = 0
    for entry in entries:
        if entry.name.lower() in covered:
            continue
        user = f"名字：{entry.name}\n描述：{entry.description}\n正文：{entry.body[:1200]}"
        try:
            raw = provider.complete(_GEN_SYS, user, json_mode=True, temperature=0.7, timeout=90.0)
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        queries = _extract_queries(raw)[:per_entry]
        if not queries:
            failed += 1
            continue
        for q in queries:
            cases.append({"query": q, "expect": [entry.name], "note": "generated"})
        made += len(queries)

    save_dataset(cases, name)
    return {"ok": True, "dataset": name, "added": made, "failed_entries": failed,
            "total": len(cases), "path": str(dataset_path(name))}


def _extract_queries(raw: str) -> List[str]:
    if not raw:
        return []
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list):
        return []
    return [str(q).strip() for q in queries if str(q).strip()]


def status(name: str = "default") -> Dict[str, Any]:
    cases = load_dataset(name)
    return {"dataset": name, "cases": len(cases), "path": str(dataset_path(name)),
            "exists": dataset_path(name).exists(),
            "generated": sum(1 for c in cases if c.get("note") == "generated"),
            "handwritten": sum(1 for c in cases if c.get("note") != "generated")}


def render(result: Dict[str, Any]) -> str:
    """把评测结果渲染成人看的表格。"""
    if not result.get("ok"):
        return result.get("message", "评测失败")
    if "delta" in result:
        lex, hyb, d = result["lexical"], result["hybrid"], result["delta"]
        lines = [f"评测集 {result['dataset']} · {lex['cases']} 条用例",
                 "",
                 f"{'指标':<12}{'纯词法':>10}{'混合':>10}{'增量':>10}",
                 "-" * 42]
        for key in [f"recall@{k}" for k in DEFAULT_K] + ["mrr"]:
            arrow = "↑" if d[key] > 0 else ("↓" if d[key] < 0 else " ")
            lines.append(f"{key:<12}{lex[key]:>10.3f}{hyb[key]:>10.3f}{d[key]:>9.3f}{arrow}")
        if not result.get("semantic_available"):
            lines += ["", "注意：当前未启用语义后端，两列必然相同。"
                          "配置方式见 ivyea memory embed。"]
        if result.get("rescued"):
            lines += ["", f"语义救回来的（词法召不回）{len(result['rescued'])} 条："]
            lines += [f"  + {q}" for q in result["rescued"][:8]]
        if result.get("regressed"):
            lines += ["", f"语义弄丢的 {len(result['regressed'])} 条（值得看一眼）："]
            lines += [f"  - {q}" for q in result["regressed"][:8]]
        return "\n".join(lines)
    corpus = result.get("corpus", 0)
    lines = [f"评测集 {result['dataset']} · {result['cases']} 条用例 · "
             f"语义 {'on' if result['semantic'] else 'off'} · 记忆库 {corpus} 条"]
    for key in [f"recall@{k}" for k in DEFAULT_K] + ["mrr"]:
        lines.append(f"  {key:<12}{result[key]:.3f}")
    lines.append(f"  未命中      {result['missed']}")
    # 语料比 k 还小时 recall@k 恒等于 1，是个没有信息量的数字，必须说破，
    # 否则很容易拿着一个"满分"去宣称检索没问题。
    trivial = [k for k in DEFAULT_K if corpus and corpus <= k]
    if trivial:
        lines.append(f"  ⚠ 记忆库只有 {corpus} 条，recall@{'/'.join(map(str, trivial))} "
                     f"恒为 1（返回全部即命中），此时只看 recall@1 和 mrr")
    misses = [d for d in result.get("details", []) if d["rank"] is None]
    if misses:
        lines += ["", "未命中的查询："]
        lines += [f"  · {m['query']}  →  期望 {'/'.join(m['expect'])}" for m in misses[:10]]
    return "\n".join(lines)
