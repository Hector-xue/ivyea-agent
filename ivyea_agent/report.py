"""报告渲染：规则引擎报告 + AI 复核 合并输出（终端 + .md 文件）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def build(rule_output: dict[str, Any], review_result: dict[str, Any]) -> str:
    parts = [rule_output.get("report_md", "").rstrip(), "\n\n---\n"]
    if review_result.get("ok") and review_result.get("markdown"):
        parts.append("# AI 复核（Ivyea Agent）\n")
        parts.append(review_result["markdown"].rstrip())
    else:
        parts.append("# AI 复核（Ivyea Agent）\n")
        parts.append(f"> {review_result.get('note', '未执行 AI 复核')}")
    parts.append(
        "\n\n---\n> ⚠️ Ivyea Agent P1 为**只读巡检**：以上为建议，"
        "不会自动改广告。执行请人工在后台操作（写操作能力见 P2）。"
    )
    return "\n".join(parts).strip() + "\n"


def write_md(text: str, output_dir: str, asin: str = "") -> str:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = f"ivyea-patrol-{asin or 'report'}.md"
    path = out / name
    path.write_text(text, encoding="utf-8")
    return str(path)
