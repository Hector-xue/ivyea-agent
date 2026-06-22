"""Local workspace indexing and project understanding helpers.

This is the general engineering-agent base layer: fast, offline, deterministic
project scans that later task/code agents can use before asking a model.
"""
from __future__ import annotations

import hashlib
import ast
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

INDEX_VERSION = 2
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

VISIBLE_HIDDEN_DIRS = {".github"}

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

IMPORT_PATTERNS = {
    "Python": [
        re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)(?:\s+as\s+\w+)?", re.M),
        re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+", re.M),
    ],
    "JavaScript": [
        re.compile(r"^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", re.M),
        re.compile(r"^\s*(?:const|let|var)\s+.*?=\s*require\(['\"]([^'\"]+)['\"]\)", re.M),
    ],
    "TypeScript": [
        re.compile(r"^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", re.M),
        re.compile(r"^\s*(?:const|let|var)\s+.*?=\s*require\(['\"]([^'\"]+)['\"]\)", re.M),
    ],
    "Go": [re.compile(r"^\s*import\s+(?:\(\s*)?[\"`]([^\"`]+)[\"`]", re.M)],
    "Rust": [re.compile(r"^\s*use\s+([A-Za-z_][\w:]*)", re.M)],
}


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
    return any(part.startswith(".") and part not in (".", "..") and part not in VISIBLE_HIDDEN_DIRS for part in rel.parts)


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


def _imports(text: str, language: str, limit: int = 80) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for pattern in IMPORT_PATTERNS.get(language, []):
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
            if len(out) >= limit:
                return out
    return out


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _python_ast(text: str, limit: int = 240) -> dict[str, Any]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {"definitions": [], "calls": []}

    definitions: list[dict[str, Any]] = []
    calls: list[str] = []
    seen_calls: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _definition(self, node: ast.AST, name: str, kind: str) -> None:
            qualname = ".".join([*self.stack, name]) if self.stack else name
            definitions.append({
                "name": name,
                "kind": kind,
                "qualname": qualname,
                "lineno": getattr(node, "lineno", 0),
                "end_lineno": getattr(node, "end_lineno", getattr(node, "lineno", 0)),
            })

        def visit_ClassDef(self, node: ast.ClassDef) -> Any:  # noqa: N802
            if len(definitions) < limit:
                self._definition(node, node.name, "class")
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:  # noqa: N802
            if len(definitions) < limit:
                self._definition(node, node.name, "function" if not self.stack else "method")
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:  # noqa: N802
            if len(definitions) < limit:
                self._definition(node, node.name, "async_function" if not self.stack else "async_method")
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
            name = _call_name(node.func)
            if name and name not in seen_calls and len(calls) < limit:
                seen_calls.add(name)
                calls.append(name)
            self.generic_visit(node)

    Visitor().visit(tree)
    return {"definitions": definitions[:limit], "calls": calls[:limit]}


JS_DEFINITION_PATTERNS = [
    ("class", re.compile(r"^\s*export\s+class\s+([A-Za-z_$][\w$]*)|^\s*class\s+([A-Za-z_$][\w$]*)", re.M)),
    ("function", re.compile(r"^\s*export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(|^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)),
    ("function", re.compile(r"^\s*export\s+const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>|^\s*const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>", re.M)),
    ("constant", re.compile(r"^\s*export\s+const\s+([A-Za-z_$][\w$]*)\s*=|^\s*const\s+([A-Za-z_$][\w$]*)\s*=", re.M)),
]

JS_CALL_PATTERN = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*\(")


def _line_no(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _javascript_semantics(text: str, limit: int = 240) -> dict[str, Any]:
    definitions: list[dict[str, Any]] = []
    seen_defs: set[str] = set()
    for kind, pattern in JS_DEFINITION_PATTERNS:
        for match in pattern.finditer(text):
            name = next((g for g in match.groups() if g), "")
            if not name or name in seen_defs:
                continue
            seen_defs.add(name)
            line = _line_no(text, match.start())
            definitions.append({
                "name": name,
                "kind": kind,
                "qualname": name,
                "lineno": line,
                "end_lineno": line,
            })
            if len(definitions) >= limit:
                break
    calls: list[str] = []
    seen_calls: set[str] = set()
    for match in JS_CALL_PATTERN.finditer(text):
        name = match.group(1)
        if name in {"if", "for", "while", "switch", "catch", "function"}:
            continue
        if name not in seen_calls:
            seen_calls.add(name)
            calls.append(name)
        if len(calls) >= limit:
            break
    return {"definitions": definitions[:limit], "calls": calls[:limit]}


def _semantic_index(text: str, language: str) -> dict[str, Any]:
    if language == "Python":
        return _python_ast(text)
    if language in {"JavaScript", "TypeScript"}:
        return _javascript_semantics(text)
    return {"definitions": [], "calls": []}


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
        language = language_for(path)
        ast_info = _semantic_index(text, language)
        entries.append({
            "path": rel,
            "size": size,
            "lines": len(text.splitlines()),
            "language": language,
            "symbols": _symbols(text),
            "imports": _imports(text, language),
            "definitions": ast_info["definitions"],
            "calls": ast_info["calls"],
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
            " ".join(d.get("qualname", "") for d in entry.get("definitions", []) or []),
            " ".join(entry.get("calls", []) or []),
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
            "definitions": entry.get("definitions", [])[:8],
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


def _module_for_path(path: str) -> str:
    if path.endswith(".py"):
        if path.endswith("/__init__.py"):
            return path[:-12].replace("/", ".")
        return path[:-3].replace("/", ".")
    stem = re.sub(r"\.(jsx?|tsx?|go|rs)$", "", path)
    return stem.replace("/", ".")


def dependency_graph(root: str | os.PathLike[str] | None = None, limit: int = 40) -> dict[str, Any]:
    idx = ensure_index(root)
    files = idx.get("files", [])
    modules = {_module_for_path(f.get("path", "")): f.get("path", "") for f in files}
    module_roots = {m.split(".", 1)[0] for m in modules if m}
    edges: list[dict[str, str]] = []
    external: Counter[str] = Counter()
    inbound: Counter[str] = Counter()
    outbound: Counter[str] = Counter()
    for f in files:
        src_path = f.get("path", "")
        for raw in f.get("imports", []) or []:
            dep = raw.lstrip(".")
            if not dep:
                continue
            target_path = ""
            if dep in modules:
                target_path = modules[dep]
            else:
                for mod, path in modules.items():
                    if dep == mod or dep.startswith(mod + ".") or mod.startswith(dep + "."):
                        target_path = path
                        break
            if target_path:
                edges.append({"from": src_path, "to": target_path, "import": raw})
                inbound[target_path] += 1
                outbound[src_path] += 1
            else:
                name = dep.split(".", 1)[0].split("/", 1)[0].split(":", 1)[0]
                if name and name not in module_roots and not raw.startswith((".", "/")):
                    external[name] += 1
    hubs = [
        {"path": path, "inbound": inbound[path], "outbound": outbound[path]}
        for path in sorted(set(inbound) | set(outbound), key=lambda p: (-(inbound[p] + outbound[p]), p))
    ][:limit]
    return {
        "root": idx.get("root"),
        "files": len(files),
        "edges": edges[:limit],
        "edge_count": len(edges),
        "external": dict(external.most_common(limit)),
        "hubs": hubs,
    }


def symbol_index(root: str | os.PathLike[str] | None = None, query: str = "", limit: int = 80) -> dict[str, Any]:
    idx = ensure_index(root)
    q = (query or "").strip().lower()
    symbols: list[dict[str, Any]] = []
    for entry in idx.get("files", []):
        for definition in entry.get("definitions", []) or []:
            name = str(definition.get("name") or "")
            qualname = str(definition.get("qualname") or name)
            if q and q not in name.lower() and q not in qualname.lower() and q not in str(entry.get("path", "")).lower():
                continue
            symbols.append({
                "path": entry.get("path", ""),
                "language": entry.get("language", ""),
                "name": name,
                "qualname": qualname,
                "kind": definition.get("kind", ""),
                "lineno": definition.get("lineno", 0),
                "end_lineno": definition.get("end_lineno", definition.get("lineno", 0)),
            })
            if len(symbols) >= limit:
                return {"root": idx.get("root"), "query": query, "symbols": symbols}
    return {"root": idx.get("root"), "query": query, "symbols": symbols}


def impact_analysis(target: str, root: str | os.PathLike[str] | None = None, limit: int = 80) -> dict[str, Any]:
    idx = ensure_index(root)
    files = idx.get("files", [])
    target_value = (target or "").strip()
    target_lower = target_value.lower()
    target_path = target_value.replace("\\", "/")
    module = _module_for_path(target_path) if "." in target_path or "/" in target_path else target_value
    definitions = symbol_index(root, target_value, limit=limit).get("symbols", [])
    direct_files: list[str] = []
    importers: list[dict[str, str]] = []
    callers: list[dict[str, str]] = []
    tests: list[str] = []

    for entry in files:
        path = str(entry.get("path", ""))
        if path == target_path or path.endswith("/" + target_path):
            direct_files.append(path)
        if _module_for_path(path) == module:
            direct_files.append(path)
        for raw in entry.get("imports", []) or []:
            dep = str(raw).lstrip(".")
            if dep == module or dep.startswith(module + ".") or module.startswith(dep + ".") or target_lower in dep.lower():
                importers.append({"path": path, "import": str(raw)})
        for call in entry.get("calls", []) or []:
            call_s = str(call)
            if target_lower and (call_s.lower() == target_lower or call_s.lower().endswith("." + target_lower)):
                callers.append({"path": path, "call": call_s})
        low = path.lower()
        if low.startswith("tests/") or "/tests/" in low or Path(path).name.startswith("test_"):
            hay = "\n".join([
                path,
                " ".join(entry.get("imports", []) or []),
                " ".join(entry.get("symbols", []) or []),
                " ".join(entry.get("calls", []) or []),
                entry.get("preview", ""),
            ]).lower()
            if target_lower and target_lower in hay:
                tests.append(path)

    affected = sorted(set(direct_files + [i["path"] for i in importers] + [c["path"] for c in callers] + tests))
    return {
        "root": idx.get("root"),
        "target": target,
        "definitions": definitions[:limit],
        "direct_files": sorted(set(direct_files))[:limit],
        "importers": importers[:limit],
        "callers": callers[:limit],
        "tests": sorted(set(tests))[:limit],
        "affected_files": affected[:limit],
        "suggested_tests": _impact_tests(tests, affected),
    }


def _impact_tests(tests: list[str], affected: list[str]) -> list[str]:
    if tests:
        return ["python -m pytest " + " ".join(sorted(set(tests))[:12])]
    if any(p.endswith(".py") for p in affected):
        return ["python -m pytest"]
    return []


def _read_indexed_file(root: Path, rel: str, max_bytes: int = 128_000) -> str:
    text = _read_text(root / rel, max_bytes)
    return text or ""


def project_inspect(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    idx = ensure_index(root)
    root_path = resolve_root(idx.get("root"))
    files = idx.get("files", [])
    paths = {f.get("path", "") for f in files}
    entrypoints: list[dict[str, str]] = []
    tests: list[str] = []
    configs: list[str] = []
    docs: list[str] = []
    risks: list[str] = []

    for path in sorted(paths):
        name = path.rsplit("/", 1)[-1]
        low = path.lower()
        if name in {"README.md", "readme.md"} or low.startswith("docs/"):
            docs.append(path)
        if name in {"pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Makefile", "Dockerfile"} or path.startswith(".github/workflows/"):
            configs.append(path)
        if low.startswith("tests/") or "/tests/" in low or name.startswith("test_") or name.endswith(".test.ts") or name.endswith(".spec.ts"):
            tests.append(path)
        if name in {"cli.py", "__main__.py", "main.py", "app.py", "server.py", "manage.py"}:
            entrypoints.append({"path": path, "kind": "conventional"})

    if "pyproject.toml" in paths:
        text = _read_indexed_file(root_path, "pyproject.toml")
        section = re.search(r"(?ms)^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)", text)
        if section:
            for match in re.finditer(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*['\"]([^'\"]+)['\"]", section.group(1), re.M):
                entrypoints.append({"path": "pyproject.toml", "kind": "script", "name": match.group(1), "target": match.group(2)})
    if "package.json" in paths:
        try:
            pkg = json.loads(_read_indexed_file(root_path, "package.json"))
            scripts = pkg.get("scripts") or {}
            for name, cmd in sorted(scripts.items())[:12]:
                entrypoints.append({"path": "package.json", "kind": "npm-script", "name": name, "target": str(cmd)})
        except json.JSONDecodeError:
            risks.append("package.json 不是合法 JSON")

    if not tests:
        risks.append("未发现测试文件")
    if not any(p.startswith(".github/workflows/") for p in paths):
        risks.append("未发现 GitHub Actions workflow")
    if any(p.endswith((".env", ".pem", ".key")) for p in paths):
        risks.append("索引中出现敏感配置/密钥类文件名，请检查 policy 与 .gitignore")

    return {
        "root": idx.get("root"),
        "generated_at": idx.get("generated_at"),
        "entrypoints": entrypoints[:24],
        "tests": tests[:24],
        "configs": configs[:24],
        "docs": docs[:24],
        "risks": risks,
        "suggested_commands": _suggest_project_commands(paths),
    }


def _suggest_project_commands(paths: set[str]) -> list[str]:
    commands: list[str] = []
    if "pyproject.toml" in paths or "pytest.ini" in paths or any(p.startswith("tests/") for p in paths):
        commands.append("python -m pytest")
    if "package.json" in paths:
        commands.append("npm test")
    if "Cargo.toml" in paths:
        commands.append("cargo test")
    if "go.mod" in paths:
        commands.append("go test ./...")
    if ".github/workflows/ci.yml" in paths or ".github/workflows/release.yml" in paths:
        commands.append("ivyea gitops ci --root .")
    return commands or ["ivyea workspace map --root ."]


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
                language = language_for(target_path)
                semantic = _semantic_index(text, language)
                entry = {
                    "path": rel,
                    "size": target_path.stat().st_size,
                    "lines": len(text.splitlines()),
                    "language": language,
                    "symbols": _symbols(text),
                    "definitions": semantic["definitions"],
                    "calls": semantic["calls"],
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
    definitions = entry.get("definitions") or []
    parts = [
        f"{entry.get('language', 'Text')} 文件",
        f"{entry.get('lines', 0)} 行",
        f"{entry.get('size', 0)} bytes",
    ]
    if definitions:
        parts.append("定义：" + ", ".join(f"{d.get('qualname')}:{d.get('lineno')}" for d in definitions[:12]))
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
        if r.get("definitions"):
            defs = ", ".join(f"{d.get('qualname')}:{d.get('lineno')}" for d in r["definitions"][:5])
            lines.append("  definitions: " + defs)
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


def render_graph(data: dict[str, Any]) -> str:
    lines = [
        "Workspace Graph",
        "",
        f"- root: {data.get('root')}",
        f"- files: {data.get('files', 0)}",
        f"- internal_edges: {data.get('edge_count', 0)}",
    ]
    external = data.get("external") or {}
    if external:
        lines.append("- external: " + ", ".join(f"{k}={v}" for k, v in list(external.items())[:12]))
    hubs = data.get("hubs") or []
    if hubs:
        lines.append("")
        lines.append("Hubs:")
        lines.extend(f"- {h['path']} inbound={h['inbound']} outbound={h['outbound']}" for h in hubs[:12])
    edges = data.get("edges") or []
    if edges:
        lines.append("")
        lines.append("Edges:")
        lines.extend(f"- {e['from']} -> {e['to']} ({e['import']})" for e in edges[:20])
    return "\n".join(lines)


def render_symbols(data: dict[str, Any]) -> str:
    symbols = data.get("symbols") or []
    lines = ["Workspace Symbols", "", f"- root: {data.get('root')}", f"- query: {data.get('query') or '*'}", f"- symbols: {len(symbols)}"]
    if symbols:
        lines.append("")
        for item in symbols:
            lines.append(
                f"- {item.get('path')}:{item.get('lineno')} "
                f"{item.get('kind')} {item.get('qualname')}"
            )
    return "\n".join(lines)


def render_impact(data: dict[str, Any]) -> str:
    lines = ["Workspace Impact", "", f"- root: {data.get('root')}", f"- target: {data.get('target')}"]
    definitions = data.get("definitions") or []
    if definitions:
        lines.extend(["", "Definitions"])
        for item in definitions[:12]:
            lines.append(f"- {item.get('path')}:{item.get('lineno')} {item.get('kind')} {item.get('qualname')}")
    if data.get("direct_files"):
        lines.extend(["", "Direct Files"])
        lines.extend(f"- {path}" for path in data["direct_files"][:20])
    if data.get("importers"):
        lines.extend(["", "Importers"])
        lines.extend(f"- {item.get('path')} imports {item.get('import')}" for item in data["importers"][:20])
    if data.get("callers"):
        lines.extend(["", "Callers"])
        lines.extend(f"- {item.get('path')} calls {item.get('call')}" for item in data["callers"][:20])
    if data.get("tests"):
        lines.extend(["", "Likely Tests"])
        lines.extend(f"- {path}" for path in data["tests"][:20])
    if data.get("suggested_tests"):
        lines.extend(["", "Suggested Tests"])
        lines.extend(f"- `{cmd}`" for cmd in data["suggested_tests"])
    return "\n".join(lines)


def render_inspect(data: dict[str, Any]) -> str:
    lines = [
        "Workspace Inspect",
        "",
        f"- root: {data.get('root')}",
        f"- generated_at: {data.get('generated_at')}",
    ]
    for title, key in (
        ("Entrypoints", "entrypoints"),
        ("Tests", "tests"),
        ("Configs", "configs"),
        ("Docs", "docs"),
        ("Risks", "risks"),
        ("Suggested commands", "suggested_commands"),
    ):
        rows = data.get(key) or []
        if not rows:
            continue
        lines.append("")
        lines.append(title + ":")
        for row in rows[:24]:
            if isinstance(row, dict):
                detail = row.get("path", "")
                if row.get("name"):
                    detail += f" · {row.get('name')}"
                if row.get("target"):
                    detail += f" -> {row.get('target')}"
                if row.get("kind"):
                    detail += f" ({row.get('kind')})"
                lines.append(f"- {detail}")
            else:
                lines.append(f"- {row}")
    return "\n".join(lines)
