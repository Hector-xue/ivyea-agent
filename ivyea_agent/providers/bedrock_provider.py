"""AWS Bedrock Converse provider.

This is a lightweight adapter for Bedrock's `converse` API.  It uses boto3 only
when the provider is selected, so normal Ivyea installs do not need AWS
dependencies unless the user chooses Bedrock.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from .base import LLMError, LLMProvider

DEFAULT_MAX_TOKENS = 4096


def resolve_region(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    return env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1"


def _require_boto3():
    try:
        import boto3
        return boto3
    except ImportError as exc:
        raise LLMError("AWS Bedrock 需要 boto3：请先安装 `pip install boto3`，并配置 AWS 凭据。") from exc


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return [{"text": " "}]
    if isinstance(content, str):
        return [{"text": content or " "}]
    if isinstance(content, list):
        blocks = []
        for item in content:
            if isinstance(item, str):
                blocks.append({"text": item or " "})
            elif isinstance(item, dict) and item.get("type") == "text":
                blocks.append({"text": str(item.get("text") or " ")})
        return blocks or [{"text": " "}]
    return [{"text": str(content)}]


def tools_to_bedrock(tools: Optional[list]) -> list[dict[str, Any]]:
    out = []
    for tool in tools or []:
        fn = tool.get("function", tool)
        out.append({
            "toolSpec": {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "inputSchema": {"json": fn.get("parameters") or {"type": "object", "properties": {}}},
            }
        })
    return out


def messages_to_bedrock(messages: list[dict]) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    system: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    tool_name_by_id: dict[str, str] = {}

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            text = msg.get("content")
            if isinstance(text, str) and text.strip():
                system.append({"text": text})
            continue

        if role == "assistant":
            blocks = []
            text = msg.get("content")
            if isinstance(text, str) and text.strip():
                blocks.append({"text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_id = str(tc.get("id") or "")
                tool_name_by_id[tool_id] = name
                blocks.append({"toolUse": {"toolUseId": tool_id, "name": name, "input": args if isinstance(args, dict) else {}}})
            append("assistant", blocks or [{"text": " "}])
            continue

        if role == "tool":
            tool_id = str(msg.get("tool_call_id") or "")
            append("user", [{
                "toolResult": {
                    "toolUseId": tool_id,
                    "content": [{"text": str(msg.get("content") or "")}],
                }
            }])
            continue

        append("user", _content_blocks(msg.get("content")))

    if not out:
        out = [{"role": "user", "content": [{"text": " "}]}]
    if out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"text": " "}]} )
    return (system or None), out


def extract_response(response: dict[str, Any]) -> dict[str, Any]:
    message = ((response.get("output") or {}).get("message") or {})
    blocks = message.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        if "text" in block:
            text_parts.append(str(block["text"]))
        if "toolUse" in block:
            tu = block["toolUse"]
            tool_calls.append({
                "id": str(tu.get("toolUseId") or ""),
                "name": str(tu.get("name") or ""),
                "arguments": tu.get("input") if isinstance(tu.get("input"), dict) else {},
            })
    usage = response.get("usage") or {}
    return {
        "role": "assistant",
        "content": "".join(text_parts),
        "tool_calls": tool_calls,
        "usage": {
            "prompt_tokens": int(usage.get("inputTokens") or 0),
            "completion_tokens": int(usage.get("outputTokens") or 0),
        },
    }


class BedrockProvider(LLMProvider):
    name = "bedrock"

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        super().__init__(api_key, model)
        self.region = resolve_region()
        self._client = None

    def _cli(self):
        if self._client is None:
            boto3 = _require_boto3()
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def complete(self, system: str, user: str, json_mode: bool = False,
                 temperature: float = 0.2, timeout: float = 60.0) -> str:
        msg = self.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ], temperature=temperature, timeout=timeout)
        return msg.get("content", "")

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        system, bedrock_messages = messages_to_bedrock(messages)
        kwargs: dict[str, Any] = {
            "modelId": self.model,
            "messages": bedrock_messages,
            "inferenceConfig": {"maxTokens": DEFAULT_MAX_TOKENS, "temperature": temperature},
        }
        if system:
            kwargs["system"] = system
        btools = tools_to_bedrock(tools)
        if btools:
            kwargs["toolConfig"] = {"tools": btools}
        try:
            return extract_response(self._cli().converse(**kwargs))
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"Bedrock Converse 调用失败：{exc}") from exc
