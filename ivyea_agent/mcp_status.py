"""MCP configuration status checks."""
from __future__ import annotations

import re
import shutil
from urllib.parse import urlparse
from typing import Any

from . import config, mcp_write


def check_server(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    security: list[str] = []
    suggestions: list[str] = []
    transport = (spec.get("transport") or "http").lower()
    if transport not in ("http", "sse", "stdio"):
        issues.append(f"unsupported_transport:{transport}")
    if transport in ("http", "sse") and not spec.get("url"):
        issues.append("missing_url")
    if transport == "stdio" and not spec.get("command"):
        issues.append("missing_command")
    if transport in ("http", "sse") and spec.get("url"):
        parsed = urlparse(str(spec.get("url") or ""))
        if parsed.scheme == "http" and (spec.get("headers") or spec.get("query")):
            security.append("auth_over_plain_http")
            suggestions.append("带鉴权的远程 MCP 建议使用 https，或仅限本机/内网可信地址。")
    if transport == "stdio" and spec.get("command"):
        command = str(spec.get("command") or "")
        if shutil.which(command) is None and not command.startswith(("/", "./", "../")):
            security.append("stdio_command_not_found")
            suggestions.append(f"确认 stdio command `{command}` 已安装并在 PATH 中，参数放在 args。")
    for location in ("headers", "query"):
        values = spec.get(location) or {}
        if isinstance(values, dict):
            for key, value in values.items():
                raw = str(value or "")
                if re.search(r"(sk-|gh[oups]_|github_pat_|Bearer\s+[A-Za-z0-9._-]{16,})", raw):
                    security.append(f"literal_secret_in_{location}:{key}")
                    suggestions.append(f"建议把 {location}.{key} 改成环境变量或本机安全配置，不要把长 token 明文放进共享配置。")
    if spec.get("dataSource"):
        ds = spec["dataSource"]
        if not isinstance(ds, dict):
            issues.append("dataSource_not_object")
        else:
            for key in ("tool", "rows_path", "field_map"):
                if not ds.get(key):
                    issues.append(f"dataSource_missing_{key}")
    write_errors = mcp_write.validate_spec(spec)
    writable = not write_errors
    if spec.get("writeActions") and write_errors:
        issues.extend(write_errors)
    if writable:
        suggestions.append("writeActions 已配置；真实写入仍必须显式传 --execute，并会走人工审批/审计。")
    elif not spec.get("writeActions"):
        suggestions.append("未配置 writeActions；该 MCP 仅作为只读数据源或工具调用使用。")
    return {
        "name": name,
        "transport": transport,
        "location": spec.get("url") or spec.get("command") or "",
        "has_auth": bool(spec.get("headers") or spec.get("query")),
        "has_data_source": bool(spec.get("dataSource")),
        "writable": writable,
        "security": security,
        "suggestions": suggestions,
        "issues": issues,
        "ok": not issues and not any(s in {"auth_over_plain_http", "stdio_command_not_found"} for s in security),
    }


def status() -> list[dict[str, Any]]:
    servers = config.load_mcp().get("mcpServers", {})
    return [check_server(name, spec) for name, spec in sorted(servers.items())]


def render(rows: list[dict[str, Any]] | None = None) -> str:
    rows = rows if rows is not None else status()
    if not rows:
        return "MCP Doctor\n\n（未配置 MCP 服务器）"
    lines = ["MCP Doctor", ""]
    for row in rows:
        issues = ", ".join(row["issues"]) if row["issues"] else "-"
        security = ", ".join(row.get("security") or []) or "-"
        lines.append(
            f"- {'OK' if row['ok'] else 'WARN'} {row['name']} "
            f"[{row['transport']}] {row['location'] or '-'} "
            f"dataSource={row['has_data_source']} writable={row['writable']} issues={issues} security={security}"
        )
        for tip in (row.get("suggestions") or [])[:3]:
            lines.append(f"  tip: {tip}")
    return "\n".join(lines)
