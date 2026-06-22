"""Gemini Code Assist OAuth provider.

This talks to Google's Cloud Code Assist backend (`cloudcode-pa`) using a
Google OAuth access token. It reuses the Gemini native request/response
translation, then wraps the request in Code Assist's envelope.
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx

from .base import LLMError
from .gemini_provider import DEFAULT_MAX_OUTPUT_TOKENS, GeminiProvider, _extract_response, _messages_to_gemini, _tools_to_gemini

CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"


class GeminiCodeAssistError(LLMError):
    def __init__(self, message: str, *, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def diagnose_gemini_code_assist_error(exc: BaseException) -> list[str]:
    if not isinstance(exc, GeminiCodeAssistError):
        return ["检查网络连接、OAuth token、Google 账号权限和 Gemini Code Assist 服务状态。"]
    body = exc.body.lower()
    status = exc.status_code
    if status in (401, 403) or "unauthorized" in body or "permission" in body:
        return [
            "OAuth token 无效或权限不足；先运行 `ivyea model auth google-gemini-cli --refresh`。",
            "如果仍失败，重新登录：`ivyea model auth google-gemini-cli --login`。",
        ]
    if status == 404 or "project" in body and "not found" in body:
        return [
            "GCP project 可能为空、拼写错误或当前账号无权访问。",
            "设置 project：`ivyea model auth google-gemini-cli --project <gcp-project-id>`。",
            "也可先用 `gcloud config get-value project` 确认本机 active project。",
        ]
    if status == 429 or "quota" in body or "rate" in body:
        return [
            "命中配额或限流；稍后重试，或切换到有配额的 Google 账号/project。",
            "必要时在 Google 控制台检查 Gemini Code Assist / Cloud AI 相关配额。",
        ]
    if "not enabled" in body or "enable" in body or "onboarding" in body:
        return [
            "账号或 project 可能尚未完成 Gemini Code Assist onboarding。",
            "请先在 Google Gemini Code Assist / Cloud Code 入口完成开通，再重新运行 `--probe`。",
        ]
    return [
        "检查 Google 账号是否开通 Gemini Code Assist、refresh token 是否失效、GCP project 是否可用。",
        "可运行 `ivyea model auth google-gemini-cli --project <id>` 固定 project 后重试。",
    ]


class GeminiCodeAssistProvider(GeminiProvider):
    name = "google-gemini-cli"

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        super().__init__(api_key, model or "gemini-3-pro-preview", base_url or "cloudcode-pa://google")
        self.project_id = ""

    def _project_id(self) -> str:
        if self.project_id:
            return self.project_id
        from .. import oauth_auth
        self.project_id = oauth_auth.google_project_id()
        return self.project_id

    def _post(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.api_key:
            raise LLMError("Google Gemini OAuth token 未配置；运行 ivyea model auth google-gemini-cli --token <access_token>")
        wrapped = {
            "project": self._project_id(),
            "model": self.model,
            "user_prompt_id": str(uuid.uuid4()),
            "request": payload,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "ivyea-agent (gemini-cli-compat)",
            "X-Goog-Api-Client": "gl-python/ivyea-agent",
            "x-activity-request-id": str(uuid.uuid4()),
        }
        try:
            resp = httpx.post(f"{CODE_ASSIST_ENDPOINT}/v1internal:generateContent",
                              json=wrapped, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise GeminiCodeAssistError(f"Gemini Code Assist 连接失败：{exc}") from exc
        if resp.status_code >= 400:
            body = resp.text[:1000]
            raise GeminiCodeAssistError(
                f"Gemini Code Assist HTTP {resp.status_code}: {body[:300]}",
                status_code=resp.status_code,
                body=body,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"Gemini Code Assist 返回非 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise LLMError("Gemini Code Assist 返回格式异常")
        return data.get("response") if isinstance(data.get("response"), dict) else data

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        system, contents = _messages_to_gemini(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": DEFAULT_MAX_OUTPUT_TOKENS,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        gtools = _tools_to_gemini(tools)
        if gtools:
            payload["tools"] = gtools
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        return _extract_response(self._post(payload, timeout))


def probe_gemini_code_assist(api_key: str, *, model: str = "gemini-3-pro-preview",
                             timeout: float = 30.0) -> dict[str, Any]:
    provider = GeminiCodeAssistProvider(api_key, model)
    result = provider.chat(
        [{"role": "user", "content": "Reply with the single word OK."}],
        temperature=0.0,
        timeout=timeout,
    )
    return {
        "ok": True,
        "model": provider.model,
        "project": provider._project_id(),
        "content": str(result.get("content") or "").strip(),
        "usage": result.get("usage") or {},
    }
