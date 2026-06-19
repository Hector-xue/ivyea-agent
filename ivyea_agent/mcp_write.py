"""Helpers for MCP write action configuration."""
from __future__ import annotations

import json
from typing import Any

REQUIRED_WRITE_ACTIONS = ("negative", "bid", "negative_rollback", "bid_rollback")

WRITE_ACTIONS_TEMPLATE: dict[str, Any] = {
    "writeActions": {
        "negative": {
            "tool": "add_negative_keyword",
            "args": {
                "keyword": "{search_term}",
                "match_type": "{negate_match}",
            },
        },
        "bid": {
            "tool": "update_bid",
            "args": {
                "keyword": "{search_term}",
                "bid": "{new_bid}",
            },
        },
        "negative_rollback": {
            "tool": "remove_negative_keyword",
            "args": {
                "keyword": "{search_term}",
            },
        },
        "bid_rollback": {
            "tool": "update_bid",
            "args": {
                "keyword": "{search_term}",
                "bid": "{current_bid}",
            },
        },
    }
}


def template_json() -> str:
    return json.dumps(WRITE_ACTIONS_TEMPLATE, ensure_ascii=False, indent=2)


def validate_spec(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    write_actions = spec.get("writeActions")
    if not isinstance(write_actions, dict):
        return ["缺少 writeActions 映射"]
    for key in REQUIRED_WRITE_ACTIONS:
        item = write_actions.get(key)
        if not isinstance(item, dict):
            errors.append(f"缺少 writeActions.{key}")
            continue
        if not item.get("tool"):
            errors.append(f"writeActions.{key}.tool 为空")
        if not isinstance(item.get("args"), dict):
            errors.append(f"writeActions.{key}.args 需为对象")
    return errors


def render_validation(name: str, spec: dict[str, Any]) -> str:
    errors = validate_spec(spec)
    if not errors:
        return f"OK MCP 写入映射 `{name}` 已配置：{', '.join(REQUIRED_WRITE_ACTIONS)}"
    lines = [f"XX MCP 写入映射 `{name}` 不完整："]
    lines.extend(f"- {e}" for e in errors)
    lines.append("可用 `ivyea mcp template` 查看 writeActions 示例。")
    return "\n".join(lines)

