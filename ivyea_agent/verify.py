"""完成前自验证门禁（重度）。

写过源码的对话轮在模型想收尾时，自动跑：
  ① 确定性静态审查 code_review.review_diff（密钥/危险命令/过宽 except…）
  ② 针对改动文件派生的 focused 测试（有对应 tests/test_<stem>*.py 才跑，避免盲跑全量）
高危发现或 focused 测试失败 → 组装反馈注回主循环，逼模型先修复/验证再收尾。
中危发现只随反馈一起呈现、不单独触发（否则几乎每次改代码都拦，过噪）。

对外只暴露 gate(root)；非 git 仓 / 无源码改动的轮次由调用方跳过。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import code_review, git_workflow

_MAX_FOCUSED = 5          # 最多跑几个 focused 测试文件，封顶运行时长
_MARK = "⚠"               # 复用 ui.tool_result 的死胡同高亮前缀


def _focused_tests(root: Path, files: list[str]) -> list[str]:
    """按改动的源码文件名映射到已存在的 tests/test_<stem>*.py。"""
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    out: list[str] = []
    for f in files:
        stem = Path(f).stem
        if not stem or f.startswith("tests/"):
            continue
        for cand in sorted(tests_dir.glob(f"test_{stem}*.py")):
            rel = cand.relative_to(root).as_posix()
            if rel not in out:
                out.append(rel)
    return out[:_MAX_FOCUSED]


def _run_focused(root: Path, tests: list[str], timeout: int) -> dict[str, Any] | None:
    """跑 focused 测试；返回失败摘要 dict 或 None(通过/未跑)。"""
    from . import code_agent
    cmd = "python -m pytest " + " ".join(tests) + " -q"
    res = code_agent.run_tests(cmd, root=root, timeout=timeout)
    if res.get("ok"):
        return None
    parsed = res.get("parsed") or {}
    return {"command": cmd,
            "failure_count": parsed.get("failure_count") or 0,
            "output": (res.get("output") or "")[-1500:]}


def gate(root: str | Path = ".", *, run_tests: bool = True, timeout: int = 120) -> dict[str, Any]:
    """完成前门禁。返回 {ok, feedback}。ok=True 放行；ok=False 时 feedback 为注回文本(⚠ 开头)。"""
    repo = git_workflow.repo_root(root)
    if not repo:
        return {"ok": True, "feedback": ""}          # 非 git 仓不拦
    repo = Path(repo)
    rev = code_review.review_diff(repo)
    if not rev.get("ok"):
        return {"ok": True, "feedback": ""}
    findings = rev.get("findings") or []
    if not rev.get("files"):
        return {"ok": True, "feedback": ""}          # 工作区无改动
    highs = [f for f in findings if f.get("severity") == "high"]
    mediums = [f for f in findings if f.get("severity") == "medium"]

    test_fail = None
    if run_tests:
        focused = _focused_tests(repo, rev.get("files") or [])
        if focused:
            test_fail = _run_focused(repo, focused, timeout)

    if not highs and not test_fail:
        return {"ok": True, "feedback": ""}           # 无高危、focused 测试通过（或无对应测试）→ 放行

    lines = [f"{_MARK} 完成前自验证发现问题，请先处理再收尾（未通过不要宣称完成）："]
    if test_fail:
        lines.append(f"· focused 测试失败（{test_fail['failure_count']} 例）：`{test_fail['command']}`")
        lines.append("  失败输出（尾部）：\n" + test_fail["output"])
    for f in highs:
        loc = f"{f.get('path')}:{f.get('line')}" if f.get("path") else ""
        lines.append(f"· [高危] {f.get('title')} {loc}——{f.get('detail')}")
    for f in mediums:                                  # 中危附带呈现，供参考
        loc = f"{f.get('path')}:{f.get('line')}" if f.get("path") else ""
        lines.append(f"· [中危] {f.get('title')} {loc}")
    lines.append("修好后再收尾；若某条确属误报，用一句话说明为什么可以放行。")
    return {"ok": False, "feedback": "\n".join(lines)}
