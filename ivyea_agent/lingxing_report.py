"""领星店铺巡检报告渲染：候选 → 终端 + .md。

按杠杆分区（否词/收割/降bid/加bid/加预算），每条带 规则/指标/理由/显著性；
被历史否决/冷却拦截的标注但仍展示（透明）。复用 report.write_md 落盘约定。
"""
from __future__ import annotations

from typing import Any

# ANSI（终端着色；写入 .md 时用 plain 版本）
_C = {"否词": "\033[33m", "收割": "\033[32m", "降bid": "\033[36m",
      "加bid": "\033[35m", "加预算": "\033[34m", "错误": "\033[31m"}
_RESET = "\033[0m"
_DIM = "\033[2m"

_LEVERS = ["否词", "收割", "降bid", "加bid", "加预算"]


def _acos(m: dict[str, Any]) -> str:
    a = m.get("acos")
    return f"{a:.0%}" if isinstance(a, (int, float)) else "—"


def _line(c: dict[str, Any]) -> str:
    m = c.get("metrics", {})
    metric = (f"{m.get('clicks', 0)}点击/{m.get('orders', 0)}单/"
              f"花费{m.get('spend', 0)}/ACOS {_acos(m)}") if m else ""
    flag = "  ⛔" + c.get("block_reason", "") if c.get("blocked") else ""
    return f"  · {c.get('target_name', '')} — {c.get('rule', '')}\n      {metric}{flag}\n      理由: {c.get('rationale', '')}"


def render(result: dict[str, Any], *, color: bool = True) -> str:
    sid = result.get("sid")
    cands = result.get("candidates", [])
    by_lever: dict[str, list] = {lv: [] for lv in _LEVERS}
    errors = []
    for c in cands:
        lv = c.get("lever")
        if lv in by_lever:
            by_lever[lv].append(c)
        elif lv == "错误":
            errors.append(c)

    ta = result.get("target_acos")
    mg = result.get("margin")
    head = [
        f"# 领星店铺巡检 — sid {sid}",
        f"> 窗口 {result.get('window_days')} 天 · {result.get('note', '')}"
        + (f" · 毛利≈{mg:.0%}" if isinstance(mg, (int, float)) else "")
        + (f" · 目标ACOS {ta:.0%}" if isinstance(ta, (int, float)) else ""),
        f"> 共 {result.get('count', 0)} 条候选（只读：以下为建议，不会自动改广告）。",
        "",
    ]
    lines = list(head)
    executable = 0
    for lv in _LEVERS:
        items = by_lever[lv]
        if not items:
            continue
        c0 = _C.get(lv, "") if color else ""
        cr = _RESET if color else ""
        lines.append(f"{c0}## {lv}（{len(items)}）{cr}")
        for c in items:
            if not c.get("blocked"):
                executable += 1
            lines.append(_line(c))
        lines.append("")
    if errors:
        lines.append("## ⚠️ 取数异常")
        for e in errors:
            lines.append(f"  · {e.get('target_name', '')}: {e.get('block_reason', '')}")
        lines.append("")
    if result.get("count", 0) == 0:
        lines.append("（窗口内无符合规则的候选——可能近期广告暂停/数据延迟，或确实健康。）")
    else:
        lines.append(f"{_DIM if color else ''}可执行候选 {executable} 条 · "
                     f"被拦截 {result.get('count', 0) - executable} 条（历史否决/冷却）。"
                     f"写入需后续里程碑的人工审批。{_RESET if color else ''}")
    return "\n".join(lines).strip() + "\n"


def render_md(result: dict[str, Any]) -> str:
    """落盘用：无 ANSI。"""
    return render(result, color=False)
