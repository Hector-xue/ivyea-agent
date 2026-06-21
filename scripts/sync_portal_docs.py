#!/usr/bin/env python3
"""Sync static portal facts from repository docs."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def replace_versions(text: str, version: str) -> str:
    return re.sub(r"v\d+\.\d+\.\d+", f"v{version}", text)


def extract_commands(markdown: str) -> list[str]:
    commands: list[str] = []
    for block in re.findall(r"```(?:bash|powershell|text)?\n(.*?)```", markdown, flags=re.S):
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("ivyea ", "curl ", "iwr ", "python scripts/")):
                commands.append(stripped)
    seen = set()
    out = []
    for cmd in commands:
        if cmd not in seen:
            seen.add(cmd)
            out.append(cmd)
    return out[:80]


def portal_data(version: str) -> dict:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deploy = (ROOT / "docs" / "部署指南.md").read_text(encoding="utf-8")
    usage = (ROOT / "docs" / "使用与操作文档.md").read_text(encoding="utf-8")
    return {
        "version": f"v{version}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": ["README.md", "docs/部署指南.md", "docs/使用与操作文档.md"],
        "commands": extract_commands("\n\n".join([readme, deploy, usage])),
        "capabilities": [
            "Amazon 广告巡检",
            "Amazon Skills / 知识库",
            "审批式写入与审计回滚",
            "Workspace 项目理解",
            "Patch / Git / CI 工作流",
            "图片 OCR 与多模态视觉",
            "安装生命周期管理",
        ],
    }


def sync_site(site_dir: Path | None = None) -> list[Path]:
    version = project_version()
    site = site_dir or ROOT / "site"
    changed: list[Path] = []
    for path in sorted(site.glob("*.html")):
        original = path.read_text(encoding="utf-8")
        updated = replace_versions(original, version)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(path)
    data_path = site / "portal-data.json"
    data_path.write_text(json.dumps(portal_data(version), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    changed.append(data_path)
    return changed


def main() -> int:
    changed = sync_site()
    print("Portal sync")
    for path in changed:
        print(f"- {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
