"""MCP configuration status checks."""
from __future__ import annotations

from typing import Any

from . import config, mcp_write


def check_server(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    transport = (spec.get("transport") or "http").lower()
    if transport not in ("http", "sse", "stdio"):
        issues.append(f"unsupported_transport:{transport}")
    if transport in ("http", "sse") and not spec.get("url"):
        issues.append("missing_url")
    if transport == "stdio" and not spec.get("command"):
        issues.append("missing_command")
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
    return {
        "name": name,
        "transport": transport,
        "location": spec.get("url") or spec.get("command") or "",
        "has_auth": bool(spec.get("headers") or spec.get("query")),
        "has_data_source": bool(spec.get("dataSource")),
        "writable": writable,
        "issues": issues,
        "ok": not issues,
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
        lines.append(
            f"- {'OK' if row['ok'] else 'WARN'} {row['name']} "
            f"[{row['transport']}] {row['location'] or '-'} "
            f"dataSource={row['has_data_source']} writable={row['writable']} issues={issues}"
        )
    return "\n".join(lines)
