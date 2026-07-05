"""Deterministic task-target resolution for engineering chat turns.

The model still decides *what* to change, but this module decides which local
repository its code-navigation tools should treat as the active workspace.
That distinction matters for screenshots: the browser/terminal hosting the
image is evidence, not automatically the program the user wants changed.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import progress_reporting


SCOPE_MARKER = "[任务范围锁定 / 执行契约]"
_PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")
_INJECTED_MARKERS = (
    "\n\n[工程上下文]",
    "\n\n[Ivyea Skill",
    "\n\n[Ivyea 内置",
    "\n\n[任务范围锁定",
)
_ENGINEERING_HINTS = (
    "代码", "项目", "仓库", "文件", "函数", "界面", "前端", "后端", "终端", "输出",
    "显示", "颜色", "高亮", "测试", "修复", "实现", "优化", "重构", "部署", "安装",
    "bug", "fix", "code", "repo", "repository", "test", "ui", "cli", "terminal",
)
_VISUAL_HINTS = ("截图", "图片", "界面", "页面", "显示", "输出", ".png", ".jpg", ".jpeg", ".webp")
_BEHAVIOR_HINTS = (
    "截图", "界面", "页面", "显示", "输出", "颜色", "高亮", "交互", "按钮", "终端", "ui",
    "ux", "render", "display", "output", "color", "terminal",
)
_CONTINUATION_HINTS = ("继续", "接着", "按这个", "照这个", "执行", "开始做", "做吧", "可以", "确认")
_COMMON_DIR_NAMES = {"root", "home", "tmp", "src", "app", "server", "client", "tests", "test"}
_STRONG_PROGRESS_HINTS = (
    "分步", "阶段", "优化方案", "实施方案", "计划模式", "复核", "全面", "全部", "批量",
    "重构", "改造", "发布", "发版", "部署", "闭环", "逐步", "详细检查", "完整检查",
)
_ACTION_PROGRESS_HINTS = (
    "优化", "修改", "修复", "实现", "执行", "创建", "检查", "分析", "调查", "整理",
    "升级", "诊断", "验证", "测试", "review", "fix", "implement", "refactor", "deploy",
)
_SEQUENCE_PROGRESS_HINTS = ("先", "然后", "接着", "再", "最后", "之后")
_EXECUTION_EXPECTED_HINTS = (
    "开始执行", "直接执行", "执行吧", "开始做", "做吧", "落地", "帮我改", "帮我修",
    "帮我实现", "请修改", "请修复", "请实现", "发版", "部署", "apply it", "implement it",
)


@dataclass
class ScopeResolution:
    project: str = ""
    root: str = ""
    confidence: str = "none"       # explicit | history | locked | workspace | none
    explicit: bool = False
    ambiguous: bool = False
    behavioral: bool = False
    visual: bool = False
    relevant: bool = False
    progress_required: bool = False
    execution_expected: bool = False
    evidence: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _clean_query(text: str) -> str:
    out = text or ""
    cuts = [out.find(marker) for marker in _INJECTED_MARKERS if marker in out]
    if cuts:
        out = out[:min(cuts)]
    return out.strip()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        rows: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"text", "input_text"} and item.get("text"):
                rows.append(str(item.get("text")))
            elif isinstance(item.get("content"), str):
                rows.append(str(item.get("content")))
        return "\n".join(rows)
    return str(content or "")


def current_user_query(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            return _clean_query(_content_text(msg.get("content")))
    return ""


def _history_user_queries(messages: list[dict[str, Any]], current: str) -> list[str]:
    rows: list[str] = []
    skipped_current = False
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        text = _clean_query(_content_text(msg.get("content")))
        if not skipped_current and text == current:
            skipped_current = True
            continue
        if text:
            rows.append(text)
        if len(rows) >= 8:
            break
    return rows


def project_root_for(path: str | os.PathLike[str] | None) -> Path | None:
    """Return the nearest project root at/above path, if one is identifiable."""
    if not path:
        return None
    try:
        p = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if p.is_file():
        p = p.parent
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return candidate
        markers = sum((candidate / marker).exists() for marker in _PROJECT_MARKERS[1:])
        if markers >= 2:
            return candidate
    return None


def _project_candidates(base: str | os.PathLike[str]) -> list[Path]:
    try:
        start = Path(base).expanduser().resolve()
    except (OSError, RuntimeError):
        return []
    own = project_root_for(start)
    found: dict[str, Path] = {}
    if own is not None:
        found[str(own)] = own
    # Always inspect the supplied base itself. Sandboxed/dev environments may
    # put a synthetic .git on /root or /tmp; that wrapper must not hide real
    # repositories immediately below it. When base is inside a real named
    # repo, also inspect that repo's siblings so an explicit new target can
    # switch the session lock.
    containers = [start]
    if own is not None and _norm(own.name) not in _COMMON_DIR_NAMES:
        containers.append(own.parent)
    seen_containers: set[str] = set()
    for container in containers:
        if str(container) in seen_containers:
            continue
        seen_containers.add(str(container))
        try:
            children = sorted(container.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            children = []
        for child in children[:200]:
            if not child.is_dir() or child.name.startswith("."):
                continue
            root = project_root_for(child)
            if root == child:
                found[str(root)] = root
    return list(found.values())


def _matches(text: str, candidates: list[Path]) -> list[Path]:
    q = _norm(text)
    compact = re.sub(r"[\s._-]+", "", (text or "").lower())
    if not q:
        return []
    matched: list[Path] = []
    for path in candidates:
        name = _norm(path.name)
        if name in _COMMON_DIR_NAMES or len(name) < 6:
            continue
        negative = any(phrase in compact for phrase in (
            f"跟{name}没关系", f"和{name}没关系", f"与{name}无关", f"{name}无关",
            f"不是{name}", f"并非{name}", f"不要改{name}", f"不改{name}", f"not{name}",
        ))
        if name in q and not negative:
            matched.append(path)
    return matched


def requires_progress_reporting(text: str) -> bool:
    """Conservatively identify work that benefits from a reporting lifecycle."""
    clean = _clean_query(text).lower()
    if not clean:
        return False
    if any(term in clean for term in _STRONG_PROGRESS_HINTS):
        return True
    actions = sum(1 for term in _ACTION_PROGRESS_HINTS if term in clean)
    sequence = sum(1 for term in _SEQUENCE_PROGRESS_HINTS if term in clean)
    return actions >= 2 or (actions >= 1 and sequence >= 2) or (actions >= 1 and len(clean) >= 60)


def _is_continuation(text: str) -> bool:
    clean = _clean_query(text).strip().lower()
    return len(clean) <= 32 and any(term in clean for term in _CONTINUATION_HINTS)


def resolve(query: str, base: str | os.PathLike[str], *, messages: list[dict[str, Any]] | None = None,
            locked_root: str = "") -> ScopeResolution:
    """Resolve one turn using current text > prior lock > recent conversation > cwd."""
    clean = _clean_query(query)
    lower = clean.lower()
    candidates = _project_candidates(base)
    result = ScopeResolution(
        behavioral=any(term in lower for term in _BEHAVIOR_HINTS),
        visual=any(term in lower for term in _VISUAL_HINTS),
        relevant=any(term in lower for term in _ENGINEERING_HINTS),
        progress_required=requires_progress_reporting(clean),
        execution_expected=any(term in lower for term in _EXECUTION_EXPECTED_HINTS),
        candidates=[str(path) for path in candidates],
    )
    current_matches = _matches(clean, candidates)
    if len(current_matches) > 1:
        result.ambiguous = True
        result.relevant = True
        result.confidence = "explicit"
        result.candidates = [str(path) for path in current_matches]
        result.evidence.append("当前指令同时命中多个本地项目")
        return result
    selected: Path | None = None
    if len(current_matches) == 1:
        selected = current_matches[0]
        result.explicit = True
        result.confidence = "explicit"
        result.evidence.append(f"当前指令明确提到 {selected.name}")
    elif locked_root:
        locked = project_root_for(locked_root)
        if locked is not None and locked.exists():
            selected = locked
            result.confidence = "locked"
            result.evidence.append("沿用本会话已确认的目标项目")
    if selected is None and messages:
        for previous in _history_user_queries(messages, clean):
            previous_matches = _matches(previous, candidates)
            if len(previous_matches) == 1:
                selected = previous_matches[0]
                result.confidence = "history"
                result.evidence.append(f"最近用户上下文指向 {selected.name}")
                break
    if selected is None:
        own = project_root_for(base)
        if own is not None:
            selected = own
            result.confidence = "workspace"
            result.evidence.append("当前工作目录位于该项目")
    if selected is not None:
        result.project = selected.name
        result.root = str(selected)
        continuation = any(term in lower for term in _CONTINUATION_HINTS)
        result.relevant = result.relevant or result.explicit or (
            result.confidence in {"history", "locked"} and (result.visual or continuation)
        )
    return result


def render_note(scope: ScopeResolution, query: str) -> str:
    if not scope.relevant and not scope.ambiguous:
        return ""
    lines = [SCOPE_MARKER]
    if scope.ambiguous:
        names = ", ".join(Path(p).name for p in scope.candidates) or "多个项目"
        lines += [f"状态：目标项目有歧义（候选：{names}）。",
                  "约束：先向用户确认目标；确认前不要调用代码搜索、写文件或执行命令。"]
        return "\n".join(lines)
    lines.append(f"目标项目：{scope.project or '未识别'}")
    lines.append(f"工具搜索根：{scope.root or '未锁定'}")
    lines.append(f"锁定依据：{'；'.join(scope.evidence) or '无'}")
    if scope.visual:
        lines.append("截图规则：浏览器域名、网页终端和外层 UI 只是承载环境；除非用户明确指定，不得把承载程序当成修改目标。")
    lines.append("执行约束：先在上述根目录定位入口/调用链并读取关键文件；若证据与锁定目标冲突，停止并澄清，不得静默换项目。")
    if scope.behavioral:
        lines.append("验收约束：这是行为/界面/输出类任务；写代码后除测试外，还必须验证一次真实运行路径或最小可运行场景。")
    if scope.progress_required:
        lines.append("汇报约束：这是复杂/多步任务；实际执行前先列 Todo 并做开始汇报，每阶段结束报告结果与证据，收尾必须汇总已做到、未做到和注意事项。")
    goal = re.sub(r"\s+", " ", _clean_query(query))[:240]
    if goal:
        lines.append("本轮目标：" + goal)
    return "\n".join(lines)


def apply_to_context(ctx: Any, scope: ScopeResolution) -> None:
    ctx.scope_ambiguous = bool(scope.ambiguous)
    ctx.behavioral_task = bool(scope.behavioral)
    if scope.root and not scope.ambiguous:
        ctx.workspace = scope.root
        ctx.target_root = scope.root
        ctx.target_project = scope.project
        ctx.target_explicit = bool(scope.explicit)
        ctx.scope_confidence = scope.confidence


def prepare_query(ctx: Any, query: str, messages: list[dict[str, Any]] | None = None,
                  *, base: str | os.PathLike[str] | None = None) -> str:
    """Resolve/apply a raw query and return the model-facing scope contract."""
    if not getattr(ctx, "workspace_base", ""):
        ctx.workspace_base = str(Path(base or getattr(ctx, "workspace", "") or os.getcwd()).expanduser().resolve())
    scope = resolve(query, ctx.workspace_base, messages=messages,
                    locked_root=getattr(ctx, "target_root", ""))
    if (getattr(ctx, "behavioral_task", False)
            and any(term in (query or "").lower() for term in _CONTINUATION_HINTS)):
        scope.behavioral = True
        scope.relevant = True
    previous_root = str(getattr(ctx, "target_root", "") or "")
    apply_to_context(ctx, scope)
    progress_reporting.prepare_context(
        ctx, query,
        required=bool(scope.progress_required),
        execution_expected=bool(scope.execution_expected),
        continuation=_is_continuation(query),
    )
    if scope.explicit or (scope.root and previous_root and Path(previous_root).resolve() != Path(scope.root).resolve()):
        ctx.search_recovery_required = False
        ctx.consecutive_search_deadends = 0
        ctx.navigation_since_read = 0
    return render_note(scope, query)


def _append_note(content: Any, note: str) -> Any:
    if not note or SCOPE_MARKER in _content_text(content):
        return content
    if isinstance(content, str):
        return content + "\n\n" + note
    if isinstance(content, list):
        rows = list(content)
        rows.append({"type": "text", "text": note})
        return rows
    return str(content or "") + "\n\n" + note


def prepare_messages(ctx: Any, messages: list[dict[str, Any]]) -> ScopeResolution:
    """Fallback used by all agent-loop entry points, including the HTTP service."""
    query = current_user_query(messages)
    note = prepare_query(ctx, query, messages)
    if note:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                msg["content"] = _append_note(msg.get("content"), note)
                break
    return resolve(query, getattr(ctx, "workspace_base", "") or getattr(ctx, "workspace", "") or os.getcwd(),
                   messages=messages, locked_root=getattr(ctx, "target_root", ""))


def adopt_project_from_path(ctx: Any, path: str | os.PathLike[str] | None) -> str:
    """Adopt concrete file/list evidence when no conflicting explicit lock exists."""
    root = project_root_for(path)
    if root is None:
        return ""
    current = str(getattr(ctx, "target_root", "") or "")
    if getattr(ctx, "target_explicit", False) and current and Path(current).resolve() != root:
        return ""
    ctx.workspace = str(root)
    ctx.target_root = str(root)
    ctx.target_project = root.name
    ctx.scope_confidence = "tool_evidence"
    return str(root)
