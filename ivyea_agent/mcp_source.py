"""MCP 数据源（通用字段映射，approach c）。

不绑死任何厂商：用 mcp.json 里每个服务器的 `dataSource` 映射把"某工具的返回"
转成规则引擎要的搜索词报告列，再落成临时 CSV 喂给规则引擎。

mcp.json 中的 dataSource 形如：
  "dataSource": {
    "tool": "get_ad_search_term_report",
    "args": {"asin": "{asin}", "site": "{site}", "days": 30},   # {asin}/{site}/{days} 运行时替换
    "rows_path": "data.rows",                                     # 指向返回里"行数组"的点路径
    "field_map": {                                                # 规则引擎列 <- 工具返回字段
      "Date": "date", "ASIN": "asin", "Campaign Name": "campaign",
      "Match Type": "match_type", "Customer Search Term": "search_term",
      "Impressions": "impressions", "Clicks": "clicks", "Spend": "spend",
      "Orders": "orders", "Sales": "sales"
    }
  }
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from . import config
from .mcp_client import MCPClient, MCPError

# 规则引擎期望的原始列（与样例 CSV 一致）
REQUIRED_COLUMNS = [
    "Date", "Brand", "ASIN", "Campaign Name", "Ad Group Name", "Targeting",
    "Match Type", "Customer Search Term", "Impressions", "Clicks", "Spend",
    "Orders", "Sales",
]


def _fill_args(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """把 args 里的 {asin}/{site}/{days} 等占位符用 ctx 替换。"""
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and "{" in v:
            try:
                out[k] = v.format(**ctx)
            except Exception:
                out[k] = v
        else:
            out[k] = v
    return out


def navigate(payload: Any, path: str) -> Any:
    """按点路径取值，如 'data.rows'；空路径返回原值。"""
    if not path:
        return payload
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def extract_payload(tool_result: dict[str, Any]) -> Any:
    """从 MCP tools/call 结果里取出真正的数据负载。

    优先 structuredContent；否则取第一个 text 内容并尝试 JSON 解析。
    """
    if not isinstance(tool_result, dict):
        return tool_result
    if tool_result.get("structuredContent") is not None:
        return tool_result["structuredContent"]
    content = tool_result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                txt = item.get("text", "")
                try:
                    return json.loads(txt)
                except Exception:
                    return txt
    return tool_result


def map_rows(rows: Any, field_map: dict[str, str]) -> list[dict[str, Any]]:
    """把工具返回的行数组按 field_map 映射成规则引擎列。缺列补空。"""
    if not isinstance(rows, list):
        raise MCPError("rows_path 指向的不是数组，请检查 dataSource.rows_path")
    mapped: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = {col: "" for col in REQUIRED_COLUMNS}
        for col, src in field_map.items():
            if col in row:
                row[col] = r.get(src, "")
        mapped.append(row)
    return mapped


def rows_to_csv(rows: list[dict[str, Any]], out_path: str) -> str:
    import pandas as pd
    df = pd.DataFrame(rows, columns=REQUIRED_COLUMNS)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def fetch_to_csv(server_name: str, asin: str, site: str, days: int = 30) -> str:
    """连 MCP → 调映射工具 → 转成 CSV，返回临时 CSV 路径。"""
    servers = config.load_mcp().get("mcpServers", {})
    spec = servers.get(server_name)
    if not spec:
        raise MCPError(f"未找到 MCP 服务器 '{server_name}'（先用 ivyea mcp add 配置）")
    ds = spec.get("dataSource")
    if not ds or not ds.get("tool") or not ds.get("field_map"):
        raise MCPError(
            f"服务器 '{server_name}' 未配置 dataSource 映射。\n"
            "先 `ivyea mcp tools " + server_name + "` 看有哪些工具，再 `ivyea mcp edit` "
            "在该服务器下补 dataSource(tool/args/rows_path/field_map)。")

    client = MCPClient(spec)
    client.initialize()
    args = _fill_args(ds.get("args", {}), {"asin": asin, "site": site, "days": days})
    result = client.call_tool(ds["tool"], args)
    payload = extract_payload(result)
    rows = navigate(payload, ds.get("rows_path", ""))
    mapped = map_rows(rows, ds["field_map"])
    if not mapped:
        raise MCPError("映射后无数据行（检查 rows_path / field_map 是否对应工具返回结构）")
    out = str(Path(tempfile.mkdtemp(prefix="ivyea_mcp_")) / f"{server_name}_{asin or 'report'}.csv")
    return rows_to_csv(mapped, out)
