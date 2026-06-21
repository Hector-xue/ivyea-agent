"""Local workspace indexing and project understanding helpers.

This is the general engineering-agent base layer: fast, offline, deterministic
project scans that later task/code agents can use before asking a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

INDEX_VERSION = 1
WORKSPACE_DIR = config.IVYEA_DIR / "workspaces"

DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".next",
    ".nuxt",
    "target",
}

DEFAULT_EXCLUDE_FILES = {
    ".DS_Store",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
}

TEXT_EXTENSIONS = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".json": "JSON",
    ".toml": "TOML",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".md": "Markdown",
    ".rst": "reStructuredText",
    ".txt": "Text",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".c": "C",
    ".h": "C/C++",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".php": "PHP",
    ".rb": "Ruby",
    ".lua": "Lua",
    ".dockerfile": "Dockerfile",
}

IMPORTANT_NAMES = {
    "README.md",
    "readme.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "Makefile",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
}

SYMBOL_PATTERNS = [
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][\w]*)\s*\(", re.M),
    re.compile(r"^\s*class\s+([A-Za-z_][\w]*)\s*[:(]", re.M),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", re.M),
]


@dataclass
class ScanOptions:
    max_files: int = 2000
    max_bytes: int = 256_000
    include_hidden: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_root(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or ".").expanduser().resolve()


def index_path(root: str | os.PathLike[str]) -> Path:
    r = resolve_root(root)
    digest = hashlib.sha1(str(r).encode("utf-8")).hexdigest()[:16]
    return WORKSPACE_DIR / f"{digest}.json"


def _is_hidden(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return any(part.startswith(".") and part not in (".", "..") for part in rel.parts)


def _skip_dir(path: Path, root: Path, include_hidden: bool) -> bool:
    if path.name in DEFAULT_EXCLUDE_DIRS:
        return True
    if path.name.endswith(".egg-info"):
        return True
    return not include_hidden and _is_hidden(path, root)


def _skip_file(path: Path, root: Path, include_hidden: bool) -> bool:
    if path.name in DEFAULT_EXCLUDE_FILES:
        return True
    if not include_hidden and _is_hidden(path, root):
        return True
    return False


def language_for(path: Path) -> str:
    name = path.name.lower()
    if name == "dockerfile":
        return "Dockerfile"
    return TEXT_EXTENSIONS.get(path.suffix.lower(), "Text")


def _looks_binary(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    if not raw:
        return False
    sample = raw[:4096]
    control = sum(1 for b in sample if b < 9 or (13 < b < 32))
    return control / max(len(sample), 1) > 0.08


def _read_text(path: Path, max_bytes: int) -> str | None:
    try:
        raw = path.read_bytes()[:max_bytes]
    except OSError:
        return None
    if _looks_binary(raw):
        return None
    return raw.decode("utf-8", errors="replace")


def _symbols(text: str, limit: int = 24) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for pattern in SYMBOL_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                out.append(name)
            if len(out) >= limit:
                return out
    return out


def _preview(text: str, max_chars: int = 700) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)[:max_chars]


def iter_files(root: Path, options: ScanOptions | None = None) -> list[Path]:
    options = options or ScanOptions()
    files: list[Path] = []
    for current, dirs, names in os.walk(root):
        cur = Path(current)
        dirs[:] = [d for d in dirs if not _skip_dir(cur / d, root, options.include_hidden)]
        for name in sorted(names):
            path = cur / name
            if _skip_file(path, root, options.include_hidden):
                continue
            if not path.is_file():
                continue
            files.append(path)
            if len(files) >= options.max_files:
                return files
    return files


def build_index(root: str | os.PathLike[str] | None = None, options: ScanOptions | None = None) -> dict[str, Any]:
    options = options or ScanOptions()
    root_path = resolve_root(root)
    entries: list[dict[str, Any]] = []
    skipped = {"binary_or_unreadable": 0, "too_large": 0}
    for path in iter_files(root_path, options):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > options.max_bytes:
            skipped["too_large"] += 1
            continue
        text = _read_text(path, options.max_bytes)
        if text is None:
            skipped["binary_or_unreadable"] += 1
            continue
        rel = path.relative_to(root_path).as_posix()
        entries.append({
            "path": rel,
            "size": size,
            "lines": len(text.splitlines()),
            "language": language_for(path),
            "symbols": _symbols(text),
            "preview": _preview(text),
        })
    return {
        "version": INDEX_VERSION,
        "root": str(root_path),
        "generated_at": _now(),
        "options": {
            "max_files": options.max_files,
            "max_bytes": options.max_bytes,
            "include_hidden": options.include_hidden,
        },
        "files": entries,
        "skipped": skipped,
    }


def save_index(index: dict[str, Any]) -> Path:
    config.ensure_dirs()
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    path = index_path(index["root"])
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_index(root: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    path = index_path(resolve_root(root))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("version") != INDEX_VERSION:
        return None
    return data


def ensure_index(root: str | os.PathLike[str] | None = None, options: ScanOptions | None = None) -> dict[str, Any]:
    existing = load_index(root)
    if existing:
        return existing
    idx = build_index(root, options)
    save_index(idx)
    return idx


def search(query: str, root: str | os.PathLike[str] | None = None, limit: int = 10) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    if not q:
        return []
    idx = ensure_index(root)
    terms = [t for t in re.split(r"\s+", q) if t]
    matches: list[dict[str, Any]] = []
    for entry in idx.get("files", []):
        hay = "\n".join([
            entry.get("path", ""),
            entry.get("language", ""),
            " ".join(entry.get("symbols", [])),
            entry.get("preview", ""),
        ]).lower()
        score = sum(hay.count(t) for t in terms)
        if not score:
            continue
        snippet = _snippet(entry.get("preview", ""), terms)
        matches.append({
            "path": entry.get("path", ""),
            "language": entry.get("language", ""),
            "lines": entry.get("lines", 0),
            "score": score,
            "symbols": entry.get("symbols", [])[:8],
            "snippet": snippet,
        })
    matches.sort(key=lambda x: (-int(x["score"]), x["path"]))
    return matches[:limit]


def _snippet(text: str, terms: list[str], max_chars: int = 260) -> str:
    if not text:
        return ""
    low = text.lower()
    pos = min([low.find(t) for t in terms if low.find(t) >= 0] or [0])
    start = max(0, pos - 80)
    return text[start:start + max_chars].replace("\n", " ")


def project_map(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    idx = ensure_index(root)
    files = idx.get("files", [])
    languages = Counter(f.get("language", "Text") for f in files)
    top_dirs: Counter[str] = Counter()
    important: list[str] = []
    by_dir: dict[str, list[str]] = defaultdict(list)
    for f in files:
        path = f.get("path", "")
        parts = path.split("/")
        top = parts[0] if len(parts) > 1 else "."
        top_dirs[top] += 1
        if path in IMPORTANT_NAMES or parts[-1] in IMPORTANT_NAMES:
            important.append(path)
        if len(by_dir[top]) < 8:
            by_dir[top].append(path)
    return {
        "root": idx.get("root"),
        "generated_at": idx.get("generated_at"),
        "file_count": len(files),
        "languages": dict(languages.most_common()),
        "top_dirs": dict(top_dirs.most_common(12)),
        "important_files": sorted(set(important))[:24],
        "sample_by_dir": dict(sorted(by_dir.items())),
        "skipped": idx.get("skipped", {}),
    }


def explain(target: str | os.PathLike[str] | None = None, root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    root_path = resolve_root(root)
    target_path = (root_path / (str(target or "."))).resolve()
    try:
        rel = target_path.relative_to(root_path).as_posix()
    except ValueError:
        rel = str(target_path)
    idx = ensure_index(root_path)
    files = idx.get("files", [])
    if target_path.is_file():
        entry = next((f for f in files if f.get("path") == rel), None)
        if not entry:
            text = _read_text(target_path, ScanOptions().max_bytes)
            if text is not None:
                entry = {
                    "path": rel,
                    "size": target_path.stat().st_size,
                    "lines": len(text.splitlines()),
                    "language": language_for(target_path),
                    "symbols": _symbols(text),
                    "preview": _preview(text),
                }
        return {
            "kind": "file",
            "target": rel,
            "summary": _file_summary(entry) if entry else "文件未在索引中，可能过大、二进制或被排除。",
            "entry": entry or {},
        }
    prefix = "" if rel == "." else rel.rstrip("/") + "/"
    scoped = [f for f in files if not prefix or str(f.get("path", "")).startswith(prefix)]
    languages = Counter(f.get("language", "Text") for f in scoped)
    symbols = []
    for f in scoped:
        for s in f.get("symbols", [])[:4]:
            symbols.append(f"{f.get('path')}::{s}")
            if len(symbols) >= 20:
                break
        if len(symbols) >= 20:
            break
    return {
        "kind": "directory",
        "target": "." if rel == "." else rel,
        "summary": f"{len(scoped)} 个可索引文件；主要语言："
        + (", ".join(f"{k} {v}" for k, v in languages.most_common(5)) or "无"),
        "languages": dict(languages.most_common()),
        "symbols": symbols,
        "files": [f.get("path") for f in scoped[:30]],
    }


def _file_summary(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "文件未在索引中。"
    symbols = entry.get("symbols") or []
    parts = [
        f"{entry.get('language', 'Text')} 文件",
        f"{entry.get('lines', 0)} 行",
        f"{entry.get('size', 0)} bytes",
    ]
    if symbols:
        parts.append("主要符号：" + ", ".join(symbols[:12]))
    return "；".join(parts)


def render_index(index: dict[str, Any], path: Path | None = None) -> str:
    files = index.get("files", [])
    langs = Counter(f.get("language", "Text") for f in files)
    lines = [
        "Workspace Index",
        "",
        f"- root: {index.get('root')}",
        f"- files: {len(files)}",
        f"- generated_at: {index.get('generated_at')}",
    ]
    if path:
        lines.append(f"- index_file: {path}")
    if langs:
        lines.append("- languages: " + ", ".join(f"{k}={v}" for k, v in langs.most_common(8)))
    skipped = index.get("skipped") or {}
    if skipped:
        lines.append("- skipped: " + ", ".join(f"{k}={v}" for k, v in skipped.items()))
    return "\n".join(lines)


def render_search(rows: list[dict[str, Any]], query: str) -> str:
    if not rows:
        return f"未找到匹配：{query}"
    lines = [f"Workspace Search: {query}", ""]
    for r in rows:
        lines.append(f"- {r['path']} ({r['language']}, score={r['score']})")
        if r.get("symbols"):
            lines.append("  symbols: " + ", ".join(r["symbols"]))
        if r.get("snippet"):
            lines.append("  " + r["snippet"])
    return "\n".join(lines)


def render_map(data: dict[str, Any]) -> str:
    lines = [
        "Workspace Map",
        "",
        f"- root: {data.get('root')}",
        f"- files: {data.get('file_count', 0)}",
        f"- generated_at: {data.get('generated_at')}",
    ]
    langs = data.get("languages") or {}
    if langs:
        lines.append("- languages: " + ", ".join(f"{k}={v}" for k, v in list(langs.items())[:10]))
    dirs = data.get("top_dirs") or {}
    if dirs:
        lines.append("- top_dirs: " + ", ".join(f"{k}={v}" for k, v in list(dirs.items())[:12]))
    important = data.get("important_files") or []
    if important:
        lines.append("")
        lines.append("Important files:")
        lines.extend(f"- {p}" for p in important)
    return "\n".join(lines)


def render_explain(data: dict[str, Any]) -> str:
    lines = [
        "Workspace Explain",
        "",
        f"- target: {data.get('target')}",
        f"- kind: {data.get('kind')}",
        f"- summary: {data.get('summary')}",
    ]
    if data.get("symbols"):
        lines.append("")
        lines.append("Symbols:")
        lines.extend(f"- {s}" for s in data["symbols"][:20])
    entry = data.get("entry") or {}
    if entry.get("preview"):
        lines.append("")
        lines.append("Preview:")
        lines.append(entry["preview"])
    return "\n".join(lines)
