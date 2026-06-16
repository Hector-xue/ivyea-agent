"""规则引擎封装。

复用 zach-search-term-report-analyzer 的确定性分析脚本（已 vendor 进
rule_engine_scripts/）。以子进程方式运行（脚本含本地相对 import，cwd 设为
脚本目录即可解析），再读回它的产物：搜索词分析.md / summary.json / 明细CSV。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent / "rule_engine_scripts"
ANALYZE = SCRIPTS_DIR / "analyze_search_term_decisions.py"
SAMPLE_CSV = SCRIPTS_DIR / "sample-search-term-report.csv"


class RuleEngineError(Exception):
    pass


def run(csv_path: str, asin: Optional[str] = None, site: str = "US",
        report_type: Optional[str] = None, output_dir: Optional[str] = None,
        timeout: int = 120) -> dict[str, Any]:
    """跑规则引擎，返回 {report_md, summary, files}。"""
    src = Path(csv_path).resolve()
    if not src.exists():
        raise RuleEngineError(f"找不到搜索词报告: {src}")
    out = Path(output_dir).resolve() if output_dir else (src.parent / "ivyea_patrol_out")
    out.mkdir(parents=True, exist_ok=True)

    args = [sys.executable, str(ANALYZE), str(src), "--output-dir", str(out), "--site", site]
    if asin:
        args += ["--asin", asin]
    if report_type:
        args += ["--report-type", report_type]
    try:
        proc = subprocess.run(
            args, cwd=str(SCRIPTS_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuleEngineError(f"规则引擎超时（{timeout}s）") from e
    if proc.returncode != 0:
        raise RuleEngineError(
            "规则引擎执行失败：\n" + (proc.stderr.decode("utf-8", "replace")[-800:]))

    # 收集产物
    report_md = ""
    summary: dict[str, Any] = {}
    files: dict[str, str] = {}
    for p in sorted(out.glob("*")):
        low = p.name.lower()
        if low.endswith(".md"):
            report_md = p.read_text(encoding="utf-8")
            files["report_md"] = str(p)
        elif low.endswith("run_summary.json"):
            try:
                summary = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                summary = {}
            files["summary_json"] = str(p)
        elif "明细" in p.name and low.endswith(".csv"):
            files["details_csv"] = str(p)
        elif "异常" in p.name and low.endswith(".csv"):
            files["anomalies_csv"] = str(p)

    if not report_md:
        raise RuleEngineError("规则引擎未生成报告（产物缺失）")
    return {"report_md": report_md, "summary": summary, "files": files,
            "stdout": proc.stdout.decode("utf-8", "replace")}
