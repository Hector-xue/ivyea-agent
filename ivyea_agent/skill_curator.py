"""技能策展：技能库长到上百条之后，谁来管。

`audit()` / `status()` 看得出技能**写得**合不合格，看不出它**该不该继续留着**。
"哪条是死的""哪两条重复了"靠翻目录就等于不管 —— 而技能库是**会长的**：内置的十来条之外，
还有外部技能根（`_extra_roots`）、agent 自己用 `skill_write` 沉淀的、以及用户手写的。
这一层是给"长起来之后"准备的，不是现在就非用不可。

（此前这里写的是"技能库已经近百条"——那是 IvyeaOps 的 Skill 中心，**不是这个仓库**。
本机 `ivyea skill list` 实际是十几条。把别处的数字当本地事实写进来，是这一批代码里
反复出现过的毛病，记在这里当反面例子。）

三条判据，全部**只出建议**
--------------------------
* **沉睡**：从来没被命中过，或很久没被命中过（`skill_usage`）。
* **重叠**：两条技能的触发词/描述高度重合 —— 它们会互相抢命中，而且改了一条另一条会漂。
* **不合格**：`audit()` 报了问题（缺触发词、正文空、知识卡指向不存在）。

**默认只打印建议，`--apply` 才动手，而且动手也只是归档（可恢复）。** 这条是硬的：
技能是用户攒下来的资产，一个自动化程序不该替他删东西。内置技能一律跳过 ——
它们随包发布，归档了下次升级又回来，纯属白折腾。
"""
from __future__ import annotations

from typing import Any

from . import skill_usage, skills

#: 多久没被命中算沉睡。
DORMANT_DAYS = 60
#: 触发词/描述重合到什么程度算重叠。Jaccard，取值凭手感但**只用来提建议**，不自动动手。
OVERLAP_THRESHOLD = 0.5
#: 一条技能至少要有几个触发词才参与重叠判定 —— 只有一两个词时重合率没有意义。
_MIN_TRIGGERS_FOR_OVERLAP = 3


def _trigger_set(sk: skills.Skill) -> set[str]:
    return {t.strip().lower() for t in sk.triggers if str(t).strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def analyze(*, dormant_days: int = DORMANT_DAYS) -> dict[str, Any]:
    """返回 {dormant, overlapping, unhealthy, total}。纯只读。"""
    user_skills = [sk for sk in skills.list_skills() if sk.scope == "user"]
    ids = [sk.id for sk in user_skills]
    by_id = {sk.id: sk for sk in user_skills}

    dormant_ids = skill_usage.dormant(ids, days=dormant_days)
    stats = skill_usage.stats()
    dormant_rows = [{
        "id": sid,
        "title": by_id[sid].title,
        "hits": int((stats.get(sid) or {}).get("hits") or 0),
        "path": by_id[sid].path,
    } for sid in dormant_ids if sid in by_id]

    overlapping: list[dict[str, Any]] = []
    candidates = [sk for sk in user_skills if len(_trigger_set(sk)) >= _MIN_TRIGGERS_FOR_OVERLAP]
    for i, left in enumerate(candidates):
        for right in candidates[i + 1:]:
            score = _jaccard(_trigger_set(left), _trigger_set(right))
            if score >= OVERLAP_THRESHOLD:
                overlapping.append({
                    "ids": [left.id, right.id],
                    "overlap": round(score, 2),
                    "shared": sorted(_trigger_set(left) & _trigger_set(right)),
                })

    unhealthy = [row for row in skills.audit()
                 if not row["ok"] and row["id"] in by_id
                 # overridden_by_user 不是毛病，是用户的明确意图
                 and row["issues"] != ["overridden_by_user"]]

    return {"dormant": dormant_rows, "overlapping": overlapping,
            "unhealthy": unhealthy, "total": len(user_skills)}


def render(report: dict[str, Any] | None = None, *, dormant_days: int = DORMANT_DAYS) -> str:
    report = report if report is not None else analyze(dormant_days=dormant_days)
    if not report["total"]:
        return "（还没有自建技能，没什么可策展的）"
    lines = [f"自建技能 {report['total']} 条。"]

    if report["dormant"]:
        lines.append(f"\n沉睡（{dormant_days} 天内没被命中过）{len(report['dormant'])} 条：")
        for row in report["dormant"]:
            lines.append(f"  · {row['id']}（命中 {row['hits']} 次）{row['title']}")
        lines.append("  建议：`ivyea skill archive <id>` 归档（可 restore，不会删）。")
    if report["overlapping"]:
        lines.append(f"\n触发词高度重合 {len(report['overlapping'])} 组（会互相抢命中）：")
        for row in report["overlapping"]:
            lines.append(f"  · {row['ids'][0]} ↔ {row['ids'][1]}"
                         f"（重合 {row['overlap']}：{'、'.join(row['shared'][:6])}）")
        lines.append("  建议：合并成一条，或把触发词分开。")
    if report["unhealthy"]:
        lines.append(f"\n有问题 {len(report['unhealthy'])} 条：")
        for row in report["unhealthy"]:
            lines.append(f"  · {row['id']}：{'、'.join(row['issues'])}")
        lines.append("  建议：补齐后重写（`skill_write` 会校验）。")
    if len(lines) == 1:
        lines.append("没有发现需要处理的技能。")
    return "\n".join(lines)


def apply_archive(report: dict[str, Any] | None = None, *,
                  dormant_days: int = DORMANT_DAYS) -> list[str]:
    """把沉睡技能归档。**只归档不删除**，且只碰用户自建的。返回归档掉的 id。

    重叠和不合格**不自动处理** —— 合并要判断哪条更好、补齐要写内容，
    这两件事没有一个自动化程序能替用户拍板。
    """
    report = report if report is not None else analyze(dormant_days=dormant_days)
    done: list[str] = []
    for row in report["dormant"]:
        try:
            skills.archive_skill(row["id"])
            done.append(row["id"])
        except (FileNotFoundError, ValueError, OSError):
            continue
    return done
