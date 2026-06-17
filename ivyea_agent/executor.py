"""执行层 —— 把已确认动作经 MCP 写工具落地（默认 dry-run），并审计、可回滚。

approach c：写操作也由 mcp.json 的 writeActions 映射驱动，不绑死厂商工具名：
  "writeActions": {
    "negative":          {"tool":"add_negative_keyword",    "args":{"keyword":"{search_term}","match_type":"{negate_match}"}},
    "negative_rollback": {"tool":"remove_negative_keyword", "args":{"keyword":"{search_term}"}},
    "bid":               {"tool":"update_bid",              "args":{"keyword":"{search_term}","bid":"{new_bid}"}},
    "bid_rollback":      {"tool":"update_bid",              "args":{"keyword":"{search_term}","bid":"{current_bid}"}}
  }
{search_term}/{new_bid}/{current_bid}/{negate_match} 等用动作字段替换。
"""
from __future__ import annotations

from typing import Any

from . import audit, config
from .actions import Action
from .mcp_client import MCPClient, MCPError


def _fill(args: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and "{" in v:
            try:
                out[k] = v.format(**fields)
            except Exception:
                out[k] = v
        else:
            out[k] = v
    return out


def _action_fields(a: Action) -> dict[str, Any]:
    return {
        "search_term": a.search_term, "match_type": a.match_type,
        "negate_match": a.negate_match, "new_bid": a.new_bid,
        "current_bid": a.current_bid, "change_pct": a.change_pct,
    }


def _write_spec(server_name: str, key: str) -> tuple[dict, dict]:
    servers = config.load_mcp().get("mcpServers", {})
    spec = servers.get(server_name)
    if not spec:
        raise MCPError(f"未找到 MCP 服务器 '{server_name}'")
    wa = (spec.get("writeActions") or {}).get(key)
    if not wa or not wa.get("tool"):
        raise MCPError(f"服务器 '{server_name}' 未配置 writeActions.{key}（ivyea mcp edit 补写映射）")
    return spec, wa


def execute(action: Action, server_name: str, dry_run: bool = True) -> dict[str, Any]:
    """执行单个动作。dry_run=True 仅预览不写。返回 {ok, dry_run, audit_id?, detail}。"""
    key = "negative" if action.kind == "negative" else "bid"
    if dry_run:
        return {"ok": True, "dry_run": True, "detail": f"[DRY-RUN] 将执行：{action.summary()}"}

    spec, wa = _write_spec(server_name, key)
    args = _fill(wa.get("args", {}), _action_fields(action))
    client = MCPClient(spec)
    client.initialize()
    try:
        result = client.call_tool(wa["tool"], args)
    except MCPError as e:
        return {"ok": False, "dry_run": False, "detail": f"写入失败：{e}"}

    aid = audit.record({
        "server": server_name, "kind": action.kind, "search_term": action.search_term,
        "tool": wa["tool"], "args": args,
        "before": {"current_bid": action.current_bid},
        "after": {"new_bid": action.new_bid, "negate_match": action.negate_match},
        "fields": _action_fields(action),
    })
    return {"ok": True, "dry_run": False, "audit_id": aid,
            "detail": f"已执行：{action.summary()}（审计 {aid}）", "result": result}


def rollback(entry_id: str) -> dict[str, Any]:
    """回滚一条审计记录（用 *_rollback 写映射 + 记录里的 before 值）。"""
    entry = audit.get(entry_id)
    if not entry:
        return {"ok": False, "detail": f"未找到审计记录 {entry_id}"}
    server_name = entry.get("server", "")
    key = "negative_rollback" if entry.get("kind") == "negative" else "bid_rollback"
    try:
        spec, wa = _write_spec(server_name, key)
    except MCPError as e:
        return {"ok": False, "detail": f"无法回滚：{e}"}
    fields = dict(entry.get("fields", {}))
    args = _fill(wa.get("args", {}), fields)
    client = MCPClient(spec)
    client.initialize()
    try:
        client.call_tool(wa["tool"], args)
    except MCPError as e:
        return {"ok": False, "detail": f"回滚写入失败：{e}"}
    audit.record({"server": server_name, "kind": "rollback", "rollback_of": entry_id,
                  "tool": wa["tool"], "args": args})
    return {"ok": True, "detail": f"已回滚 {entry_id}：{entry.get('kind')} {entry.get('search_term')}"}
