"""Provider-neutral multimodal vision request packages."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from . import image_audit
from .security import redact_obj


DEFAULT_MODELS = {
    "openai": "gpt-5-mini",
    "anthropic": "claude-sonnet-4-6",
    "gemini": "gemini-2.5-flash",
}


def clipboard_image() -> str | None:
    """从系统剪贴板取图片，保存为临时 png，返回路径；无图/不支持返回 None。
    best-effort 三平台：macOS(pngpaste)、Linux(xclip/wl-paste)、Windows(PowerShell)。
    仅本地真终端可用；网页终端无法访问系统剪贴板。"""
    import subprocess
    import sys
    import tempfile
    from . import config
    try:
        config.ensure_dirs()
        out = str(Path(tempfile.mkdtemp(prefix="ivyea-paste-")) / "clip.png")
    except Exception:
        out = str(Path(tempfile.gettempdir()) / "ivyea-clip.png")
    plat = sys.platform
    cmds: list[list[str]] = []
    if plat == "darwin":
        cmds = [["pngpaste", out]]
    elif plat.startswith("linux"):
        cmds = [["bash", "-lc", f"wl-paste --type image/png > {out!r}"],
                ["bash", "-lc", f"xclip -selection clipboard -t image/png -o > {out!r}"]]
    elif plat.startswith("win"):
        ps = ("$img=Get-Clipboard -Format Image; if($img){{$img.Save('{p}')}} "
              "else {{exit 3}}").format(p=out)
        cmds = [["powershell", "-NoProfile", "-Command", ps]]
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    return None

API_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _image_paths(result: dict[str, Any], limit: int = 8) -> list[str]:
    return [i["path"] for i in result.get("images", [])[:limit]]


def build(provider: str, paths: list[str], *, product_context: str = "",
          model: str = "", max_images: int = 8) -> dict[str, Any]:
    """Build a dry-run multimodal request package.

    The package is intentionally serializable and does not call external APIs.
    """
    provider = (provider or "openai").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError("provider 仅支持 openai / anthropic / gemini")
    result = image_audit.audit(paths)
    prompt = image_audit.multimodal_prompt(result, product_context=product_context)
    images = _image_paths(result, limit=max_images)
    model = model or DEFAULT_MODELS[provider]
    payload = _payload(provider, model, prompt, images)
    return {
        "provider": provider,
        "model": model,
        "images": images,
        "prompt": prompt,
        "local_audit": result,
        "payload": payload,
    }


def build_general(provider: str, paths: list[str], *, task: str = "", context: str = "",
                  model: str = "", max_images: int = 8) -> dict[str, Any]:
    """Build a generic screenshot/report/UI multimodal request package."""
    provider = (provider or "openai").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError("provider 仅支持 openai / anthropic / gemini")
    result = image_audit.audit(paths)
    images = _image_paths(result, limit=max_images)
    model = model or DEFAULT_MODELS[provider]
    prompt = generic_prompt(result, task=task, context=context)
    payload = _payload(provider, model, prompt, images)
    return {
        "provider": provider,
        "model": model,
        "images": images,
        "prompt": prompt,
        "local_audit": result,
        "payload": payload,
        "mode": "general",
    }


def generic_prompt(result: dict[str, Any], *, task: str = "", context: str = "") -> str:
    images = result.get("images", [])
    lines = [
        "你是 Ivyea Agent 的通用多模态视觉分析器。",
        "",
        "请基于图片内容完成用户任务，优先输出可执行结论，不要编造图片中不存在的信息。",
        "",
        "## 用户任务",
        task or "识别图片/截图/报表中的关键信息、异常、风险和下一步建议。",
    ]
    if context:
        lines.extend(["", "## 上下文", context])
    lines.extend(["", "## 本地图片预检查"])
    if not images:
        lines.append("- 未发现可用图片。")
    for img in images[:12]:
        lines.append(
            f"- {img.get('path')} size={img.get('width')}x{img.get('height')} "
            f"bytes={img.get('bytes')} role={img.get('role')}"
        )
    lines.extend([
        "",
        "## 输出格式",
        "1. 先给结论摘要。",
        "2. 列出图片中直接可见的证据。",
        "3. 标出不确定点和需要人工确认的内容。",
        "4. 给出下一步动作清单。",
    ])
    return "\n".join(lines)


def _payload(provider: str, model: str, prompt: str, images: list[str]) -> dict[str, Any]:
    if provider == "openai":
        return {
            "model": model,
            "input": [{
                "role": "user",
                "content": (
                    [{"type": "input_text", "text": prompt}] +
                    [{"type": "input_image", "image_url": image_audit.data_url(p)} for p in images]
                ),
            }],
        }
    if provider == "anthropic":
        content = [{"type": "text", "text": prompt}]
        for p in images:
            data_url = image_audit.data_url(p)
            mime, b64 = data_url.split(",", 1)
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime.split(":", 1)[1].split(";", 1)[0],
                    "data": b64,
                },
            })
        return {"model": model, "max_tokens": 3000, "messages": [{"role": "user", "content": content}]}
    if provider == "gemini":
        parts = [{"text": prompt}]
        for p in images:
            data_url = image_audit.data_url(p)
            mime, b64 = data_url.split(",", 1)
            parts.append({"inline_data": {
                "mime_type": mime.split(":", 1)[1].split(";", 1)[0],
                "data": b64,
            }})
        return {"model": model, "contents": [{"role": "user", "parts": parts}]}
    raise ValueError(f"未知 provider: {provider}")


def render_package(pkg: dict[str, Any], *, include_payload: bool = False) -> str:
    lines = [
        "# 多模态视觉请求包",
        "",
        f"- provider: {pkg['provider']}",
        f"- model: {pkg['model']}",
        f"- images: {len(pkg['images'])}",
        "",
        "## 图片",
    ]
    if not pkg["images"]:
        lines.append("（无图片，无法进行真实视觉审核）")
    else:
        for p in pkg["images"]:
            lines.append(f"- {p}")
    lines.extend(["", "## Prompt", "", pkg["prompt"]])
    if include_payload:
        redacted = json.loads(json.dumps(pkg["payload"], ensure_ascii=False))
        _truncate_images(redacted)
        lines.extend(["", "## Payload Preview", "", "```json", json.dumps(redacted, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines) + "\n"


def _truncate_images(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in ("image_url", "data") and isinstance(v, str) and len(v) > 120:
                obj[k] = v[:80] + "...<base64 truncated>"
            else:
                _truncate_images(v)
    elif isinstance(obj, list):
        for item in obj:
            _truncate_images(item)


def write_package(pkg: dict[str, Any], path: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    safe = json.loads(json.dumps(pkg, ensure_ascii=False))
    _truncate_images(safe)
    p.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def _api_key(provider: str, override: str = "") -> str:
    if override:
        return override
    env = API_ENV_KEYS.get(provider, "")
    return os.environ.get(env, "") if env else ""


def _endpoint(provider: str, model: str) -> str:
    if provider == "openai":
        return "https://api.openai.com/v1/responses"
    if provider == "anthropic":
        return "https://api.anthropic.com/v1/messages"
    if provider == "gemini":
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    raise ValueError(f"未知 provider: {provider}")


def _headers(provider: str, api_key: str) -> dict[str, str]:
    if provider == "openai":
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if provider == "anthropic":
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    if provider == "gemini":
        return {"Content-Type": "application/json"}
    raise ValueError(f"未知 provider: {provider}")


def _extract_text(provider: str, data: dict[str, Any]) -> str:
    if provider == "openai":
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        chunks: list[str] = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                text = content.get("text") or content.get("output_text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks).strip()
    if provider == "anthropic":
        chunks = [c.get("text", "") for c in data.get("content", []) or [] if c.get("type") == "text"]
        return "\n".join(chunks).strip()
    if provider == "gemini":
        chunks = []
        for cand in data.get("candidates", []) or []:
            for part in (cand.get("content") or {}).get("parts", []) or []:
                if isinstance(part.get("text"), str):
                    chunks.append(part["text"])
        return "\n".join(chunks).strip()
    return ""


def call(
    pkg: dict[str, Any],
    *,
    api_key: str = "",
    timeout: float = 120.0,
) -> dict[str, Any]:
    provider = pkg["provider"]
    key = _api_key(provider, api_key)
    if not key:
        env = API_ENV_KEYS.get(provider, "")
        return {"ok": False, "provider": provider, "error": f"缺少 API key：请设置 {env} 或传 --api-key。"}
    url = _endpoint(provider, pkg["model"])
    headers = _headers(provider, key)
    params = {"key": key} if provider == "gemini" else None
    try:
        resp = httpx.post(url, headers=headers, params=params, json=pkg["payload"], timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        return {"ok": False, "provider": provider, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "provider": provider, "error": f"响应不是 JSON：{exc}"}
    return {
        "ok": True,
        "provider": provider,
        "model": pkg["model"],
        "text": _extract_text(provider, data),
        "raw": redact_obj(data),
    }


def render_call(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"视觉模型调用失败：provider={result.get('provider', '-')} error={result.get('error', '-')}\n"
    text = result.get("text") or "（模型未返回可抽取文本，查看 raw 字段）"
    return (
        "# 多模态视觉审核结果\n\n"
        f"- provider: {result.get('provider')}\n"
        f"- model: {result.get('model')}\n\n"
        "## 结论\n\n"
        f"{text}\n"
    )


# ── 视觉旁路：主脑不支持图片时，用已配置的视觉模型 sidecar 代读、文本回灌主线 ──

SIDECAR_PROMPT = (
    "你是视觉转述助手。用户正与一个没有视觉能力的模型对话并附上了图片。"
    "请围绕用户的问题客观转述图片内容：逐字转录可见文本与数字、表格数据、图表趋势、"
    "UI/版面结构、商品图卖点与瑕疵、异常点。不要替后续模型回答问题，"
    "只提供回答该问题所需的全部视觉信息。用中文。"
)


def main_has_vision(mcfg: dict) -> bool:
    """主脑是否自带视觉能力（按 provider 能力位判定，离线保守）。"""
    from . import models
    prov = models.provider_by_id(str(mcfg.get("provider_id") or mcfg.get("provider") or ""))
    if not prov:
        return False
    return bool(models.provider_capabilities(prov).get("vision"))


def pick_vision_model() -> dict | None:
    """选 sidecar 视觉模型：config `vision_model`（provider id 或模型 id）优先；
    否则取第一个有 vision 能力且已配 key 的 provider。返回可喂 from_settings 的
    {cfg, key, label}；无可用返回 None。"""
    from . import config, models
    config.load_env()

    def _from_provider(p: dict) -> dict | None:
        if not models.provider_capabilities(p).get("vision"):
            return None
        key = os.environ.get(str(p.get("key_env") or ""), "")
        if not key:
            return None
        model = p.get("default_model") or next(iter(p.get("models") or []), "")
        cfg2 = {"kind": p.get("kind"), "provider_id": p.get("id"),
                "api_mode": p.get("api_mode", ""), "auth_type": p.get("auth_type", "api_key"),
                "model": model, "base": p.get("base", ""), "base_url": p.get("base", ""),
                "key_env": p.get("key_env", ""), "label": p.get("label") or p.get("id")}
        return {"cfg": cfg2, "key": key, "label": f"{cfg2['label']} · {model}"}

    prefer = str(config.get_setting("vision_model", "") or "").strip()
    if prefer:
        m = models.by_id(prefer)
        if m:
            prov = models.provider_by_id(str(m.get("provider_id") or ""))
            if prov and models.provider_capabilities(prov).get("vision"):
                key = os.environ.get(str(m.get("key_env") or ""), "")
                if key:
                    cfg2 = {"kind": m.get("kind"), "provider_id": m.get("provider_id"),
                            "api_mode": m.get("api_mode", ""), "auth_type": m.get("auth_type", "api_key"),
                            "model": m.get("model"), "base": m.get("base", ""), "base_url": m.get("base", ""),
                            "key_env": m.get("key_env", ""), "label": m.get("label") or prefer}
                    return {"cfg": cfg2, "key": key, "label": f"{cfg2['label']}"}
        p = models.provider_by_id(prefer)
        if p:
            got = _from_provider(p)
            if got:
                return got
    for p in models.providers():
        got = _from_provider(p)
        if got:
            return got
    return None


def sidecar_describe(images: list[str], question: str, picked: dict) -> str:
    """用选定的视觉模型代读图片，返回分析文本。异常向上抛，由 route_images 兜底。"""
    from . import mentions
    from .providers import from_settings
    provider = from_settings(picked["cfg"], picked["key"])
    msg = provider.chat([
        {"role": "system", "content": SIDECAR_PROMPT},
        {"role": "user", "content": mentions.build_user_content(
            f"用户的问题：{question or '（无文字，仅图片）'}", images)},
    ], tools=None)
    return (msg.get("content") or "").strip()


def route_images(user_content: str, images: list[str], mcfg: dict, narrate) -> tuple[str, list[str]]:
    """视觉旁路总入口：主脑有 vision 或本轮无图 → 原样返回；
    否则 sidecar 分析注入文本、剥掉图片；无可用视觉模型/出错 → warn 后忽略图片继续（fail-open）。"""
    from . import ui
    if not images or main_has_vision(mcfg):
        return user_content, images
    picked = pick_vision_model()
    if not picked:
        narrate(ui.message("warn", "主脑不支持图片且没有可用的视觉模型（配 OPENAI/ANTHROPIC/GEMINI key，"
                                   "或 ivyea config set vision_model <provider>），本轮忽略图片仅按文字继续。"))
        return user_content, []
    try:
        analysis = sidecar_describe(images, user_content, picked)
    except Exception as e:  # noqa: BLE001
        narrate(ui.message("warn", f"视觉旁路调用失败（{e}），本轮忽略图片仅按文字继续。"))
        return user_content, []
    if not analysis:
        narrate(ui.message("warn", "视觉模型未返回内容，本轮忽略图片仅按文字继续。"))
        return user_content, []
    narrate(ui.message("info", f"主脑不支持图片，已由 {picked['label']} 视觉旁路代读 {len(images)} 张图"))
    injected = (user_content
                + f"\n\n[图片内容（主脑不支持图片，已由视觉模型 {picked['label']} 代读，共 {len(images)} 张）]\n"
                + analysis)
    return injected, []
