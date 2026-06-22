"""Ivyea Agent CLI 入口。

P1 子命令：
  ivyea config show
  ivyea config set <key> <value>
  ivyea patrol <搜索词报告.csv> [--asin B0..] [--site US] [--target-acos 0.3] [--no-llm]
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config, ui


def _ask(prompt: str, default: str = "") -> str:
    """问一行；回车=用默认。"""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return val or default


def _ask_secret(prompt: str) -> str:
    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _model_needs_key(settings: dict | None = None) -> bool:
    s = settings or config.load_settings()
    auth = (s.get("auth_type") or "api_key").lower()
    if auth in ("none", "aws_sdk"):
        return False
    if auth in ("oauth_external", "oauth_device_code", "copilot"):
        return True
    return bool(s.get("key_env"))


def _model_ready(settings: dict | None = None) -> bool:
    s = settings or config.load_settings()
    if not _model_needs_key(s):
        return True
    return bool(config.get_active_key())


def _model_key_label(settings: dict | None = None) -> str:
    s = settings or config.load_settings()
    auth = (s.get("auth_type") or "api_key").lower()
    if auth == "none":
        return "无需 key"
    if auth == "aws_sdk":
        return "AWS SDK 凭据"
    if auth in ("oauth_external", "oauth_device_code", "copilot"):
        provider_id = str(s.get("provider_id") or s.get("provider") or "")
        if config.get_active_key():
            return f"已认证({auth})"
        suffix = s.get("key_env") or provider_id or "auth-token"
        return f"未认证({suffix})"
    return "已配 key" if config.get_active_key() else f"未配 key({s.get('key_env') or '-'})"


def _model_picker() -> None:
    """像 Hermes：按 provider profile 列出模型、认证状态和可用性。"""
    from . import models
    config.ensure_dirs()
    s = config.load_settings()
    print(f"\n当前主脑: {_C['c']}{s.get('label', s.get('provider'))}{_C['x']} "
          f"（{s.get('model')}，{_model_key_label(s)}）\n")
    idx, n = {}, 1
    for group, items in models.grouped():
        print(f"{_C['b']}{group}{_C['x']}")
        for m in items:
            status = m.get("status", "usable")
            auth = m.get("auth_type", "api_key")
            tag = "" if status == "usable" else f"{_C['d']} ({auth}·待接){_C['x']}"
            print(f"  {_C['c']}{n:>2}{_C['x']}) {m['label']}{tag}")
            idx[str(n)] = m; n += 1
    choice = _ask("\n选择编号（回车取消）")
    m = idx.get(choice)
    if not m:
        print("已取消。"); return
    model, base = m.get("model", ""), m.get("base", "")
    if m.get("provider_id") in ("custom", "azure-foundry") or m["id"] == "custom":
        base = _ask("base_url（OpenAI 兼容，如 https://xxx/v1）", base)
        model = _ask("model 名", model)
    elif m["kind"] == "openai":
        model = _ask("model 名（回车用默认）", model)
    config.apply_model(m, model=model, base_url=base)
    if m.get("key_env") and m.get("auth_type", "api_key") == "api_key":
        cur = "已配置" if config.get_active_key() else "未配置"
        nk = _ask_secret(f"{m['label']} 的 API key（{m['key_env']}，当前{cur}；回车跳过 / - 清空）")
        if nk == "-":
            config.set_env_key(m["key_env"], "")
        elif nk:
            config.set_env_key(m["key_env"], nk)
        print(f"✓ 已切换主脑：{m['label']}（{model or m.get('model')}），"
              f"{'已配 key' if config.get_active_key() else '未配 key'}")
    else:
        if m.get("status") == "usable":
            print(f"✓ 已切换主脑：{m['label']}（{model or m.get('model')}），{m.get('auth_type', 'api_key')}")
        else:
            print(f"已选 {m['label']}，但{m.get('note', '该 provider 需要后续 transport/认证适配')}。")


def _render_model_providers() -> str:
    from . import models
    config.load_env()
    lines = ["Model Providers", ""]
    for group, items in models.grouped_providers():
        lines.append(group)
        for p in items:
            status = p.get("status", "usable")
            state = models.key_status(p)
            models_preview = ", ".join((p.get("models") or [])[:4]) or "(自填)"
            lines.append(
                f"  {p['id']:<18} {status:<8} {p.get('auth_type', '-'):<18} "
                f"{state:<24} {models_preview}"
            )
        lines.append("")
    lines.append("提示：API key / OpenAI-compatible / Claude API / Gemini API / Gemini Code Assist / Bedrock / Copilot / Codex Responses / 本地端点已可用；Qwen OAuth 支持 Qwen CLI 登录导入；Gemini Code Assist 支持 OAuth 登录和 project 保存。")
    return "\n".join(lines).rstrip()


def _render_model_doctor() -> str:
    from . import models
    s = config.load_settings()
    provider_id = s.get("provider_id", s.get("provider", ""))
    p = models.provider_by_id(provider_id) or {}
    lines = ["Model Doctor", ""]
    rows = [
        ("provider", provider_id or "-"),
        ("label", s.get("label", "-")),
        ("model", s.get("model", "-")),
        ("kind", s.get("kind", "-")),
        ("api_mode", s.get("api_mode", "-")),
        ("auth_type", s.get("auth_type", "-")),
        ("base_url", s.get("base_url", "-")),
        ("key", _model_key_label(s)),
    ]
    lines.append(ui.kv(rows, color=False))
    if p.get("status") and p.get("status") != "usable":
        lines.append("")
        lines.append(f"! provider 尚未可用：{p.get('note') or '需要后续 transport/认证适配'}")
    elif _model_needs_key(s) and not config.get_active_key():
        lines.append("")
        lines.append(f"! 缺少 API key：请设置 {s.get('key_env')}，或运行 ivyea model 重新配置。")
    else:
        lines.append("")
        lines.append("OK 当前模型配置可进入对话。")
    return "\n".join(lines)


def _render_model_auth() -> str:
    from . import models
    lines = ["Model Auth", ""]
    for p in models.providers():
        auth = (p.get("auth_type") or "api_key").lower()
        if auth not in ("oauth_external", "oauth_device_code", "copilot"):
            continue
        lines.append(
            f"{p['id']:<20} {p.get('status', 'usable'):<8} "
            f"{auth:<18} {models.key_status(p):<28} {p.get('label', '')}"
        )
    if len(lines) == 2:
        lines.append("（暂无 OAuth/Copilot provider）")
    lines.append("")
    lines.append("Qwen：  ivyea model auth qwen-oauth --login")
    lines.append("        ivyea model auth qwen-oauth --device-code")
    lines.append("        ivyea model auth qwen-oauth --token <access_token>")
    lines.append("        ivyea model auth qwen-oauth --import-qwen-cli")
    lines.append("Codex： ivyea model auth openai-codex --device-code")
    lines.append("        ivyea model auth openai-codex --probe")
    lines.append("Gemini: ivyea model auth google-gemini-cli --login")
    lines.append("        ivyea model auth google-gemini-cli --login --no-browser")
    lines.append("        ivyea model auth google-gemini-cli --project <gcp-project-id>")
    lines.append("        ivyea model auth google-gemini-cli --probe")
    lines.append("        ivyea model auth google-gemini-cli --token <access_token> --refresh-token <refresh_token>")
    lines.append("Copilot: ivyea model auth copilot --exchange")
    lines.append("         ivyea model auth copilot --probe")
    lines.append("退出：  ivyea model logout <provider>")
    return "\n".join(lines).rstrip()


def _format_expires_at(value: object) -> str:
    try:
        expires_at = int(float(value or 0))
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at))


def _render_auth_detail(provider_id: str, provider: dict) -> str:
    from . import models, oauth_auth
    item = oauth_auth.get_auth(provider_id)
    rows = [
        ("provider", provider_id),
        ("label", provider.get("label", "-")),
        ("auth_type", provider.get("auth_type", "-")),
        ("status", models.key_status(provider)),
        ("token", "present" if item.get("access_token") else "missing"),
        ("refresh_token", "present" if item.get("refresh_token") else "missing"),
        ("expires_at", _format_expires_at(item.get("expires_at"))),
        ("source", item.get("source") or "-"),
        ("auth_file", str(oauth_auth.auth_path())),
    ]
    if provider_id == "google-gemini-cli":
        rows.append(("gcp_project", oauth_auth.google_project_id() or "(empty project)"))
    return ui.kv(rows, color=False)


def _cmd_model_auth(args: argparse.Namespace, action: str) -> int:
    from . import models, oauth_auth
    provider_id = args.extra
    if not provider_id:
        print(_render_model_auth())
        return 0
    provider = models.provider_by_id(provider_id)
    if not provider:
        print(f"未知 provider：{provider_id}。`ivyea model providers` 看清单。", file=sys.stderr)
        return 2
    auth = (provider.get("auth_type") or "api_key").lower()
    if auth not in ("oauth_external", "oauth_device_code", "copilot"):
        print(f"{provider_id} 使用 {auth}，无需 `model auth`；API key 请写入 {provider.get('key_env') or '.env'}。")
        return 0
    if action == "logout":
        existed = oauth_auth.clear_auth(provider_id)
        print(f"{provider_id}: {'已清除本地认证' if existed else '本地没有认证记录'}")
        return 0
    if getattr(args, "project", None) is not None:
        if provider_id != "google-gemini-cli":
            print("--project 目前只适用于 google-gemini-cli", file=sys.stderr)
            return 2
        oauth_auth.set_google_project_id(args.project)
        project = oauth_auth.google_project_id()
        print(f"{provider_id}: 已保存 GCP project = {project or '(空 project)'}")
        if not any(getattr(args, flag, False) for flag in ("refresh", "login", "device_code", "exchange", "import_qwen_cli", "probe")) \
                and not getattr(args, "token", None):
            return 0
    if getattr(args, "refresh", False):
        if provider_id not in ("qwen-oauth", "openai-codex", "google-gemini-cli"):
            print("--refresh 目前支持 qwen-oauth / openai-codex / google-gemini-cli", file=sys.stderr)
            return 2
        try:
            if provider_id == "qwen-oauth":
                oauth_auth.refresh_qwen_token()
            elif provider_id == "openai-codex":
                oauth_auth.refresh_codex_token()
            else:
                oauth_auth.refresh_google_token()
        except oauth_auth.OAuthAuthError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{provider_id}: 已刷新本地认证（token 已隐藏）")
        return 0
    if getattr(args, "login", False):
        if provider_id not in ("google-gemini-cli", "qwen-oauth"):
            print("--login 目前支持 google-gemini-cli / qwen-oauth", file=sys.stderr)
            return 2
        try:
            if provider_id == "google-gemini-cli":
                oauth_auth.google_oauth_login(open_browser=not getattr(args, "no_browser", False), notify=print)
            else:
                oauth_auth.qwen_cli_login()
        except oauth_auth.OAuthAuthError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{provider_id}: 已完成 OAuth 登录（token 已隐藏，文件 {oauth_auth.auth_path()}）")
        if provider_id == "google-gemini-cli":
            print(f"{provider_id}: GCP project = {oauth_auth.google_project_id() or '(空 project)'}")
        return 0
    if getattr(args, "device_code", False):
        if provider_id not in ("openai-codex", "qwen-oauth"):
            print("--device-code 目前支持 openai-codex / qwen-oauth", file=sys.stderr)
            return 2
        try:
            if provider_id == "openai-codex":
                oauth_auth.codex_device_code_login(notify=print)
            else:
                print("提示：Qwen 官方文档说明 Qwen OAuth free tier 已于 2026-04-15 停用；该流程仅用于仍可用账号/缓存兼容。")
                oauth_auth.qwen_device_code_login(open_browser=not getattr(args, "no_browser", False), notify=print)
        except oauth_auth.OAuthAuthError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{provider_id}: 已保存本地认证（token 已隐藏，文件 {oauth_auth.auth_path()}）")
        if provider_id == "openai-codex":
            print("提示：Codex Responses transport 已接入；选择 openai-codex:<model> 后可作为主脑使用。")
        return 0
    if getattr(args, "exchange", False):
        if provider_id != "copilot":
            print("--exchange 目前只适用于 copilot", file=sys.stderr)
            return 2
        try:
            oauth_auth.resolve_copilot_api_token(strict=True)
        except oauth_auth.OAuthAuthError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{provider_id}: GitHub token 已成功换取 Copilot API token（token 已隐藏）")
        print("提示：Copilot chat/completions transport 已接入；选择 copilot:<model> 后可作为主脑使用。")
        return 0
    if getattr(args, "import_qwen_cli", False):
        if provider_id != "qwen-oauth":
            print("--import-qwen-cli 只适用于 qwen-oauth", file=sys.stderr)
            return 2
        try:
            src = oauth_auth.import_qwen_cli_tokens()
        except oauth_auth.OAuthAuthError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{provider_id}: 已从 {src} 导入本地认证（token 已隐藏，文件 {oauth_auth.auth_path()}）")
        return 0
    if getattr(args, "token", None):
        oauth_auth.set_auth_token(
            provider_id,
            args.token.strip(),
            refresh_token=(getattr(args, "refresh_token", None) or "").strip(),
            expires_at=getattr(args, "expires_at", 0) or 0,
            source="manual",
        )
        print(f"{provider_id}: 已保存本地认证（token 已隐藏，文件 {oauth_auth.auth_path()}）")
        if provider_id == "google-gemini-cli":
            print(f"{provider_id}: GCP project = {oauth_auth.google_project_id() or '(空 project)'}")
        if not getattr(args, "probe", False):
            return 0
    if getattr(args, "probe", False):
        if provider_id not in ("google-gemini-cli", "openai-codex", "copilot", "qwen-oauth"):
            print("--probe 目前支持 google-gemini-cli / openai-codex / copilot / qwen-oauth", file=sys.stderr)
            return 2
        token = oauth_auth.resolve_provider_token(provider_id, provider.get("key_env", ""), refresh=True)
        if not token:
            print(f"{provider_id} token 未配置；先运行 `ivyea model auth {provider_id}` 查看登录方式。", file=sys.stderr)
            return 1
        try:
            from .providers.base import LLMError
            if provider_id == "google-gemini-cli":
                from .providers.gemini_code_assist_provider import probe_gemini_code_assist
                result = probe_gemini_code_assist(token, model=provider.get("default_model", "gemini-3-pro-preview"),
                                                  timeout=getattr(args, "timeout", 30.0) or 30.0)
            elif provider_id == "openai-codex":
                from .providers.codex_provider import probe_codex
                result = probe_codex(token, model=provider.get("default_model", "gpt-5.3-codex"),
                                     base_url=provider.get("base", "https://chatgpt.com/backend-api/codex"),
                                     timeout=getattr(args, "timeout", 30.0) or 30.0)
            elif provider_id == "qwen-oauth":
                from .providers.openai_compat import probe_openai_compat
                result = probe_openai_compat(token, model=provider.get("default_model", "qwen3.7-max"),
                                             base_url=provider.get("base", "https://portal.qwen.ai/v1"),
                                             timeout=getattr(args, "timeout", 30.0) or 30.0)
            else:
                from .providers.copilot_provider import probe_copilot
                result = probe_copilot(token, model=provider.get("default_model", "gpt-4o"),
                                       base_url=provider.get("base", "https://api.githubcopilot.com"),
                                       timeout=getattr(args, "timeout", 30.0) or 30.0)
        except (oauth_auth.OAuthAuthError, LLMError, OSError) as exc:
            print(f"{provider_id} probe 失败：{exc}", file=sys.stderr)
            if provider_id == "google-gemini-cli":
                from .providers.gemini_code_assist_provider import diagnose_gemini_code_assist_error
                for hint in diagnose_gemini_code_assist_error(exc):
                    print(f"排查：{hint}", file=sys.stderr)
            elif provider_id == "openai-codex":
                print("排查：确认 Codex device-code 登录未失效、账号具备 Codex 访问权限，并尝试 `ivyea model auth openai-codex --refresh`。", file=sys.stderr)
            elif provider_id == "qwen-oauth":
                print("排查：确认 Qwen CLI/Portal token 未失效，或尝试 `ivyea model auth qwen-oauth --refresh` / `--import-qwen-cli`。", file=sys.stderr)
            else:
                print("排查：确认 GH_TOKEN/GITHUB_TOKEN 可用于 Copilot，classic PAT(ghp_*) 不支持；可先运行 `ivyea model auth copilot --exchange`。", file=sys.stderr)
            return 1
        rows = [
            ("model", result.get("model", "-")),
            ("response", result.get("content") or "-"),
            ("usage", json.dumps(result.get("usage") or {}, ensure_ascii=False)),
        ]
        if provider_id == "google-gemini-cli":
            rows.insert(1, ("gcp_project", result.get("project") or "(empty project)"))
        print(f"{provider_id} probe 成功")
        print(ui.kv(rows, color=False))
        return 0
    status = models.key_status(provider)
    print(f"{provider_id}: {status}")
    if auth in ("oauth_external", "oauth_device_code", "copilot"):
        print(_render_auth_detail(provider_id, provider))
    if provider.get("status") != "usable":
        print(provider.get("note") or "该 provider 需要专用 OAuth/transport，尚未可用。")
        return 1
    if provider_id == "qwen-oauth":
        print("如果已安装 Qwen CLI，可一键登录并导入：")
        print("  ivyea model auth qwen-oauth --login")
        print("也可直接运行 Qwen OAuth device-code 流程（官方免费层已于 2026-04-15 停用，可能被服务端拒绝）：")
        print("  ivyea model auth qwen-oauth --device-code")
        print("可先从已授权环境取得 Bearer token 后导入：")
        print("  ivyea model auth qwen-oauth --token <access_token>")
        print("如果本机已登录 Qwen CLI，可直接导入：")
        print("  ivyea model auth qwen-oauth --import-qwen-cli")
        print("已有 refresh token 时可手动刷新：")
        print("  ivyea model auth qwen-oauth --refresh")
        print("验证 Qwen Portal chat/completions 是否真实可用：")
        print("  ivyea model auth qwen-oauth --probe")
        print("也可以设置环境变量 QWEN_API_KEY，优先级高于 auth.json。")
        return 0
    if provider_id == "openai-codex":
        print("可运行 OpenAI Codex device-code 登录：")
        print("  ivyea model auth openai-codex --device-code")
        print("已有 refresh token 时可手动刷新：")
        print("  ivyea model auth openai-codex --refresh")
        print("验证 Codex OAuth Responses 是否真实可用：")
        print("  ivyea model auth openai-codex --probe")
        print("提示：Codex Responses transport 已接入；选择 openai-codex:<model> 后可作为主脑使用。")
        return 0
    if provider_id == "copilot":
        print("可先配置 COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN。")
        print("支持 gho_、github_pat_、ghu_；不支持 classic PAT(ghp_*)。")
        print("验证并换取 Copilot API token：")
        print("  ivyea model auth copilot --exchange")
        print("验证 Copilot chat/completions 是否真实可用：")
        print("  ivyea model auth copilot --probe")
        print("提示：Copilot chat/completions transport 已接入；选择 copilot:<model> 后可作为主脑使用。")
        return 0
    if provider_id == "google-gemini-cli":
        print("可运行浏览器 OAuth 登录：")
        print("  ivyea model auth google-gemini-cli --login")
        print("服务器/SSH 环境可用手动粘贴模式：")
        print("  ivyea model auth google-gemini-cli --login --no-browser")
        print("如需固定 Google Cloud project，可保存：")
        print("  ivyea model auth google-gemini-cli --project <gcp-project-id>")
        print("验证 token/project/onboarding/配额是否真实可用：")
        print("  ivyea model auth google-gemini-cli --probe")
        print("可先导入 Google OAuth access token / refresh token：")
        print("  ivyea model auth google-gemini-cli --token <access_token> --refresh-token <refresh_token>")
        print("已有 refresh token 时可手动刷新：")
        print("  ivyea model auth google-gemini-cli --refresh")
        print(f"当前 GCP project：{oauth_auth.google_project_id() or '(空 project；将按 Code Assist 可用性尝试)'}")
        print("提示：Gemini Code Assist transport 已接入；可作为主脑使用。")
        return 0
    print(provider.get("note") or "该 provider 需要导入 token 或配置对应环境变量。")
    return 0


def _cmd_onboard(args: argparse.Namespace = None) -> int:
    """首次运行引导：三步配好就能用。"""
    config.ensure_dirs()
    print(f"{_C['c']}{_C['b']}{_BANNER}{_C['x']}")
    print(f"\n{_C['b']}欢迎使用 Ivyea Agent{_C['x']} —— 自托管的亚马逊运营对话式 Agent。")
    print(f"{_C['d']}三步配好就能用；任何一步都可回车跳过，之后用 ivyea model / lingxing setup 再配。{_C['x']}\n")

    print(f"{_C['b']}① 选主脑模型并配 API key{_C['x']}（推荐 DeepSeek：便宜、工具调用稳）")
    _model_picker()

    print(f"\n{_C['b']}② 接领星 OpenAPI（可选，拉真实广告数据/写回）{_C['x']}")
    if _ask("现在配领星吗？(y/N)").strip().lower().startswith("y"):
        host = _ask("OpenAPI Host", config.get_setting("lingxing_openapi_host", "https://openapi.lingxing.com"))
        appid = _ask("appId", config.get_setting("lingxing_openapi_appid", ""))
        secret = _ask_secret("appSecret（回车跳过）")
        config.set_setting("lingxing_openapi_host", host.strip())
        config.set_setting("lingxing_openapi_appid", appid.strip())
        if secret:
            config.set_env_key("LINGXING_OPENAPI_SECRET", secret.strip())
        print(f"{_C['d']}已存。`ivyea lingxing probe` 可自检；`ivyea lingxing sellers` 查店铺。{_C['x']}")
    else:
        print(f"{_C['d']}（跳过；无领星也能用 CSV：ivyea patrol 报告.csv）{_C['x']}")

    print(f"\n{_C['b']}③ 写账户打法到 AGENTS.md（可选，长期指令，对话自动注入）{_C['x']}")
    if _ask("现在生成 AGENTS.md 模板吗？(y/N)").strip().lower().startswith("y"):
        from . import memory
        created, p = memory.init_agents(str(config.IVYEA_DIR / "AGENTS.md"))
        print(f"{_C['d']}{'已生成 ' + p + '，填好后对话自动遵守。' if created else '已存在 ' + p}{_C['x']}")

    ready = "已配 key，可直接对话" if config.get_active_key() else "未配 key，对话前请 ivyea model 配置"
    print(f"\n{_C['g']}✓ 配置完成（{ready}）。{_C['x']}")
    print(f"{_C['d']}敲 {_C['c']}ivyea{_C['d']} 进对话；{_C['c']}/help{_C['d']} 看命令。{_C['x']}")
    return 0


def _config_wizard() -> int:
    """配置向导：站点 + 目标 ACoS + 模型选择（含密钥）。"""
    config.ensure_dirs()
    s = config.load_settings()
    print("── Ivyea Agent 配置向导（回车=保留当前）──")
    config.set_setting("site", _ask("默认站点", s.get("site", "US")))
    acos_raw = _ask("目标 ACoS (如 0.3)", str(s.get("target_acos", 0.3)))
    try:
        config.set_setting("target_acos", float(acos_raw))
    except ValueError:
        print(f"  (target_acos '{acos_raw}' 非数字，保留 {s.get('target_acos')})")
    _model_picker()
    print("\n✓ 配置已保存。\n")
    return _print_config()


def _print_config() -> int:
    s = config.load_settings()
    servers = config.load_mcp().get("mcpServers", {})
    print(ui.panel("当前配置", ui.kv([
        ("配置目录", config.IVYEA_DIR),
        (".env", f"{config.ENV_FILE} ({'存在' if config.ENV_FILE.exists() else '缺失'})"),
        ("mcp.json", f"{config.MCP_FILE} ({'存在' if config.MCP_FILE.exists() else '缺失'})"),
        ("主脑模型", f"{s.get('label', s.get('provider'))} · {s.get('model')} · kind={s.get('kind')}"
                 f" · provider={s.get('provider_id', '-')} · {_model_key_label(s)}"),
        ("站点", f"{s.get('site')} · 目标 ACoS {s.get('target_acos')}"),
        ("MCP 服务器", ', '.join(servers) if servers else "(无，用 ivyea mcp add 添加)"),
    ]), kind="info"))
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    if args.action is None:
        return _config_wizard()
    if args.action == "edit":
        default_editor = "notepad" if sys.platform.startswith("win") else "nano"
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or default_editor
        config.ENV_FILE.touch(exist_ok=True)
        target = config.SETTINGS_FILE if args.key == "settings" else config.ENV_FILE
        try:
            subprocess.call([editor, str(target)])
        except FileNotFoundError:
            print(f"找不到编辑器 '{editor}'。设置环境变量 EDITOR 后重试，或直接编辑: {target}",
                  file=sys.stderr)
            return 1
        return 0
    if args.action == "show":
        return _print_config()
    if args.action == "set":
        if not args.key or args.value is None:
            print("用法: ivyea config set <key> <value>", file=sys.stderr)
            return 2
        val: object = args.value
        if args.key == "target_acos":
            try:
                val = float(args.value)
            except ValueError:
                print("target_acos 需为数字，如 0.3", file=sys.stderr)
                return 2
        config.set_setting(args.key, val)
        print(f"已设置 {args.key} = {val}")
        return 0
    return 2


def _mcp_add_wizard() -> int:
    """对话式添加一个 MCP 服务器，写入 ~/.ivyea/mcp.json。"""
    import shlex
    print("── 添加 MCP 服务器（回车=用默认）──\n")
    name = _ask("名称（如 lingxing / sorftime / sif）")
    if not name:
        print("已取消（名称为空）。", file=sys.stderr)
        return 2
    transport = (_ask("传输方式 http/sse/stdio", "http") or "http").lower()
    spec: dict = {"transport": transport}
    if transport in ("http", "sse"):
        url = _ask("服务器 URL")
        if not url:
            print("已取消（URL 为空）。", file=sys.stderr)
            return 2
        spec["url"] = url
        auth = (_ask("鉴权方式 none/header/query", "none") or "none").lower()
        if auth == "header":
            hname = _ask("Header 名", "Authorization")
            hval = _ask_secret("Header 值（如 Bearer sk-...）")
            spec["headers"] = {hname: hval}
        elif auth == "query":
            qname = _ask("URL 参数名", "key")
            qval = _ask_secret("参数值")
            spec["query"] = {qname: qval}
    elif transport == "stdio":
        cmd = _ask("启动命令（含参数，如：npx -y some-mcp）")
        if not cmd:
            print("已取消（命令为空）。", file=sys.stderr)
            return 2
        parts = shlex.split(cmd)
        spec["command"], spec["args"] = parts[0], parts[1:]
    else:
        print(f"不支持的传输方式: {transport}", file=sys.stderr)
        return 2

    config.mcp_set_server(name, spec)
    safe = {k: ("***" if k in ("headers", "query") else v) for k, v in spec.items()}
    print(f"\n✓ 已保存 MCP 服务器 '{name}' → {config.MCP_FILE}")
    print(f"  {safe}")
    print("  （P1.5 的 MCP 客户端将读取它直连拉数据；当前为配置就绪）")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    if args.action == "add":
        return _mcp_add_wizard()
    if args.action == "list":
        servers = config.load_mcp().get("mcpServers", {})
        if not servers:
            print("(无 MCP 服务器，用 `ivyea mcp add` 添加)")
            return 0
        for name, spec in servers.items():
            t = spec.get("transport", "?")
            loc = spec.get("url") or spec.get("command", "")
            auth = "header" if spec.get("headers") else ("query" if spec.get("query") else "none")
            print(f"  {name}\t[{t}]\t{loc}\t鉴权:{auth}")
        return 0
    if args.action == "template":
        from . import mcp_write
        print(mcp_write.template_json())
        return 0
    if args.action == "doctor":
        from . import mcp_status
        rows = mcp_status.status()
        print(mcp_status.render(rows))
        return 1 if any(not r["ok"] for r in rows) else 0
    if args.action == "validate":
        from . import mcp_write
        if not args.name:
            print("用法: ivyea mcp validate <名称>", file=sys.stderr)
            return 2
        servers = config.load_mcp().get("mcpServers", {})
        spec = servers.get(args.name or "")
        if not spec:
            print(f"未找到 MCP 服务器 '{args.name}'（先 ivyea mcp add）", file=sys.stderr)
            return 2
        errors = mcp_write.validate_spec(spec)
        print(mcp_write.render_validation(args.name, spec))
        return 1 if errors else 0
    if args.action == "remove":
        if not args.name:
            print("用法: ivyea mcp remove <名称>", file=sys.stderr)
            return 2
        ok = config.mcp_remove_server(args.name)
        print("已删除。" if ok else f"未找到服务器 '{args.name}'。")
        return 0 if ok else 1
    if args.action == "edit":
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or \
            ("notepad" if sys.platform.startswith("win") else "nano")
        if not config.MCP_FILE.exists():
            config.save_mcp(config.load_mcp())
        try:
            subprocess.call([editor, str(config.MCP_FILE)])
        except FileNotFoundError:
            print(f"找不到编辑器；直接编辑: {config.MCP_FILE}", file=sys.stderr)
            return 1
        return 0
    if args.action in ("tools", "call", "suggest"):
        from .mcp_client import MCPClient, MCPError
        from . import mcp_source
        servers = config.load_mcp().get("mcpServers", {})
        spec = servers.get(args.name or "")
        if not spec:
            print(f"未找到 MCP 服务器 '{args.name}'（先 ivyea mcp add）", file=sys.stderr)
            return 2
        try:
            client = MCPClient(spec)
            client.initialize()
            if args.action == "tools":
                tools = client.list_tools()
                if not tools:
                    print("(该服务器未返回工具)")
                    return 0
                print(f"'{args.name}' 暴露 {len(tools)} 个工具：\n")
                for t in tools:
                    print(f"● {t.get('name')}")
                    if t.get("description"):
                        print(f"    {t['description'][:160]}")
                    props = ((t.get("inputSchema") or {}).get("properties") or {})
                    if props:
                        print(f"    入参: {', '.join(props.keys())}")
                print("\n提示：用 `ivyea mcp call " + (args.name or "<名称>") +
                      " <工具> --args '{...}'` 看返回结构，再 `ivyea mcp edit` 填 dataSource 映射。")
                return 0
            # call / suggest
            if not args.tool:
                print("用法: ivyea mcp call|suggest <名称> <工具> [--args '{\"k\":\"v\"}']", file=sys.stderr)
                return 2
            arguments = {}
            if args.args:
                try:
                    arguments = __import__("json").loads(args.args)
                except Exception as e:
                    print(f"--args 不是合法 JSON: {e}", file=sys.stderr)
                    return 2
            res = client.call_tool(args.tool, arguments)
            if args.action == "suggest":
                suggestion = mcp_source.suggest_data_source(args.tool, arguments, res)
                print(mcp_source.render_suggestion(suggestion))
                return 0
            print(__import__("json").dumps(res, ensure_ascii=False, indent=2)[:4000])
            return 0
        except MCPError as e:
            print(f"[MCP 错误] {e}", file=sys.stderr)
            return 1
    return 2


def _cmd_patrol(args: argparse.Namespace) -> int:
    from . import patrol as patrol_mod
    from . import profiles
    from .rule_engine import RuleEngineError

    if getattr(args, "from_lingxing", False):
        return _patrol_lingxing(args)

    csv_path = args.csv
    if args.from_mcp:
        from .mcp_source import fetch_to_csv
        from .mcp_client import MCPError
        if not args.asin:
            print("--from-mcp 需要 --asin（按 ASIN 拉广告搜索词数据）", file=sys.stderr)
            return 2
        profile = profiles.resolve(asin=args.asin or "")
        site = args.site or profile.get("site") or config.get_setting("site", "US")
        try:
            print(ui.message("info", f"MCP 从 '{args.from_mcp}' 拉取 {args.asin} 近 {args.days} 天广告数据..."), file=sys.stderr)
            csv_path = fetch_to_csv(args.from_mcp, args.asin, site, days=args.days)
            print(ui.message("success", f"MCP 已拉取并转换为: {csv_path}"), file=sys.stderr)
        except MCPError as e:
            print(ui.message("error", f"MCP 错误: {e}"), file=sys.stderr)
            return 1
    if not csv_path:
        print("需要提供搜索词报告 CSV，或用 --from-mcp <服务器> 自动拉数。", file=sys.stderr)
        return 2
    try:
        args.csv = csv_path
        profile = profiles.resolve(asin=args.asin or "")
        result = patrol_mod.patrol(
            args.csv, asin=args.asin, site=args.site or profile.get("site"),
            target_acos=args.target_acos if args.target_acos is not None else profile.get("target_acos"),
            report_type=args.report_type, output_dir=args.output_dir, use_llm=not args.no_llm)
    except RuleEngineError as e:
        print(f"[规则引擎错误] {e}", file=sys.stderr)
        return 1
    print(result["text"])
    print("\n" + ui.message("success", f"报告已保存: {result['md_path']}"), file=sys.stderr)
    if not result["review"]["ok"] and not args.no_llm:
        print(ui.message("warn", result["review"]["note"]), file=sys.stderr)
    return 0


def _cmd_diagnose(args: argparse.Namespace) -> int:
    from . import account_diagnosis as ad, profiles, report

    profile = profiles.resolve(asin=args.asin or "")
    try:
        result = ad.diagnose(
            args.csv,
            target_acos=args.target_acos if args.target_acos is not None
            else float(profile.get("target_acos") or config.get_setting("target_acos", 0.3)),
            listing_text=args.listing_text or "",
            min_clicks_no_order=args.min_clicks_no_order,
            top_n=args.top,
        )
    except Exception as e:
        print(ui.message("error", f"诊断失败: {e}"), file=sys.stderr)
        return 1
    text = ad.render_md(result)
    print(text)
    if args.output_dir:
        md_path = report.write_md(text, args.output_dir, asin="account")
        print("\n" + ui.message("success", f"诊断报告已保存: {md_path}"), file=sys.stderr)
    return 0


def _patrol_lingxing(args: argparse.Namespace) -> int:
    """领星 OpenAPI 店铺巡检（只读，sid 维度规则引擎）。"""
    from . import lingxing_optimizer as opt, lingxing_report as lrep, report
    from .lingxing_openapi import LingXingError, is_configured

    if not is_configured():
        print("未配置领星 OpenAPI。先运行 `ivyea lingxing setup`（填 host/appid/secret）。", file=sys.stderr)
        return 2
    if not args.sid:
        print("--from-lingxing 需要 --sid <店铺SID>（用 `ivyea lingxing sellers` 查）。", file=sys.stderr)
        return 2
    def _progress(label, i, total):
        bar = "█" * (i * 16 // total) + "░" * (16 - i * 16 // total)
        sys.stderr.write(f"\r[领星] {label} {bar} {i}/{total} 天")
        sys.stderr.flush()
        if i == total:
            sys.stderr.write("\n")
    try:
        print(ui.message("info", f"领星店铺 sid={args.sid} 拉取近 {args.days} 天广告报表（逐日聚合，已缓存的秒回）..."),
              file=sys.stderr)
        result = opt.run_store(int(args.sid), days=args.days, progress=_progress)
    except LingXingError as e:
        print(ui.message("error", f"领星错误: {e}"), file=sys.stderr)
        return 1
    print(lrep.render(result, color=sys.stdout.isatty()))
    out_dir = args.output_dir or str(config.IVYEA_DIR / "patrol_out")
    md_path = report.write_md(lrep.render_md(result), out_dir, asin=f"sid{args.sid}")
    print("\n" + ui.message("success", f"报告已保存: {md_path}"), file=sys.stderr)

    from . import shadow
    n = shadow.record(args.sid, result.get("candidates", []))   # 影子台账：记建议
    if n:
        print(f"{_C['d']}[影子] 已记录 {n} 条建议入台账，过些天 `ivyea shadow report --sid {args.sid}` 看若照做的收益。{_C['x']}", file=sys.stderr)

    if getattr(args, "execute", False):
        if shadow.shadow_mode():
            print(f"{_C['d']}[影子模式] 只记不写——已记录建议，未执行任何写操作。{_C['x']}", file=sys.stderr)
            return 0
        return _execute_lingxing_candidates(result, yes=getattr(args, "yes", False))
    return 0


def _execute_lingxing_candidates(result: dict, yes: bool = False) -> int:
    """对巡检候选逐条人工审批 → 写入（默认 dry-run；真写需 operate 开关）。"""
    from . import lingxing_write as lw, permission

    writable = []
    for c in result.get("candidates", []):
        if c.get("blocked"):
            continue
        intent = lw.candidate_to_intent(c)
        if intent and all(intent.get(k) is not None for k in ("sid",)):
            writable.append(intent)
    if not writable:
        print("没有可写入的候选（收割为建议项、被拦截项不写）。", file=sys.stderr)
        return 0

    live = lw.operate_active()
    print(f"\n共 {len(writable)} 个可写动作。operate 开关：{'开（将真实写入）' if live else '关（dry-run 预览）'}。",
          file=sys.stderr)
    state = permission.PermissionState()
    done = 0
    for intent in writable:
        if not yes:
            decision = permission.request_intent(intent, lw.preview(intent), state)
            if decision == permission.ABORT:
                print("已全部停止。", file=sys.stderr)
                break
            if decision == permission.DENY:
                from . import memory
                memory.record_decision(f"sid:{intent.get('sid')}",
                                       intent.get("keyword_text") or str(intent.get("target_name")),
                                       lw._kind_for_memory(intent["op_type"]), "reject")
                print(f"  跳过：{lw.preview(intent)}", file=sys.stderr)
                continue
        r = lw.execute(intent, dry_run=not live)
        print(("  ✓ " if r["ok"] else "  ✗ ") + r["detail"])
        if r["ok"] and not r.get("dry_run"):
            done += 1
    if not live:
        print("\n（以上为 dry-run 预览。真实写入需 `ivyea lingxing operate on`。）", file=sys.stderr)
    elif done:
        print(f"\n已写入 {done} 条。回滚用 `ivyea audit rollback <ID>`。", file=sys.stderr)
    return 0


def _cmd_lingxing(args: argparse.Namespace) -> int:
    from . import lingxing_openapi as lx
    from .lingxing_datasets import list_sellers
    from .lingxing_openapi import LingXingError

    if args.action == "setup":
        print("配置领星 OpenAPI（凭据只存本机 ~/.ivyea/）：")
        host = _ask("OpenAPI Host", config.get_setting("lingxing_openapi_host", "https://openapi.lingxing.com"))
        appid = _ask("appId", config.get_setting("lingxing_openapi_appid", ""))
        secret = _ask_secret("appSecret（回车保留原值）")
        config.set_setting("lingxing_openapi_host", host.strip())
        config.set_setting("lingxing_openapi_appid", appid.strip())
        if secret:
            config.set_env_key("LINGXING_OPENAPI_SECRET", secret.strip())
        print("已保存。运行 `ivyea lingxing probe` 自检。")
        return 0
    if args.action == "probe":
        try:
            r = lx.verify()
        except LingXingError as e:
            print(f"[领星自检失败] {e}", file=sys.stderr)
            return 1
        print(f"✓ 令牌获取成功；店铺列表 code={r['probe_code']}，店铺数={r['probe_seller_count']}")
        return 0
    if args.action == "sellers":
        try:
            sellers = list_sellers()
        except LingXingError as e:
            print(f"[领星错误] {e}", file=sys.stderr)
            return 1
        print(f"共 {len(sellers)} 个店铺：")
        for s in sellers:
            print(f"  sid={s.get('sid')}  {s.get('name')}  {s.get('country') or ''}")
        return 0
    if args.action == "cache":
        from . import lingxing_cache
        if (args.value or "").lower() == "clear":
            print(f"已清空领星缓存（{lingxing_cache.clear()} 条）。")
        else:
            print("用法：ivyea lingxing cache clear")
        return 0
    if args.action == "operate":
        from . import lingxing_write as lw
        sub = (args.value or "status").lower()
        if sub == "on":
            lw.set_operate(True)
            print("⚠️ 领星写入开关已开启（默认 120 分钟后自动关）。写动作仍需逐条人工审批。")
        elif sub == "off":
            lw.set_operate(False)
            print("领星写入开关已关闭（回到 dry-run）。")
        else:
            print(f"领星写入开关：{'开' if lw.operate_active() else '关'}")
        return 0
    return 2


def _cmd_shadow(args: argparse.Namespace) -> int:
    from . import shadow
    if args.action == "on":
        shadow.set_shadow(True); print("影子模式已开：巡检只记建议、不写广告。攒几天用 shadow report 看收益。"); return 0
    if args.action == "off":
        shadow.set_shadow(False); print("影子模式已关：可正常审批写入（仍需 operate 开关）。"); return 0
    if args.action == "list":
        rows = shadow.list_recs(args.sid or "", limit=50)
        if not rows:
            print("（影子台账为空，先跑几次 ivyea patrol --from-lingxing）"); return 0
        import time as _t
        for r in rows:
            print(f"  {_t.strftime('%m-%d', _t.localtime(r['ts']))} sid{r['sid']} {r['lever']}「{r['target']}」"
                  f" {r['clicks']:.0f}点击/{r['orders']:.0f}单/¥{r['spend']:.2f}")
        return 0
    # report
    if not args.sid:
        print("用法：ivyea shadow report --sid <店铺SID> [--days 14]", file=sys.stderr); return 2
    from . import lingxing_optimizer as opt
    from .lingxing_openapi import LingXingError, is_configured
    if not is_configured():
        print("未配置领星 OpenAPI，无法拉现况回测。", file=sys.stderr); return 2
    recs = shadow.list_recs(str(args.sid), limit=500)
    if not recs:
        print("该店铺影子台账为空。"); return 0
    try:
        print(f"[影子] 拉取 sid={args.sid} 近 {args.days} 天搜索词现况回测…", file=sys.stderr)
        current = opt.aggregate_terms(int(args.sid), days=args.days)
    except LingXingError as e:
        print(f"[领星错误] {e}", file=sys.stderr); return 1
    result = shadow.evaluate(recs, current)
    print(shadow.summary_text(str(args.sid), result))
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    from . import actions as act_mod, executor, guardrails, memory, profiles
    from pathlib import Path

    detail = args.source
    asin = ""
    if Path(args.source).is_dir():
        detail = act_mod.load_detail_from_dir(args.source)
        asin = act_mod.asin_from_dir(args.source)
    if not detail or not Path(detail).exists():
        print(f"找不到巡检明细 CSV（传入巡检输出目录或 *明细*.csv）：{args.source}", file=sys.stderr)
        return 2

    profile = profiles.resolve(asin=asin)
    protected = [w for w in (args.protected or "").split(",") if w.strip()]
    protected += list(profile.get("protected_terms") or [])
    acts = guardrails.annotate(act_mod.extract_actions(detail, asin=asin), protected_terms=protected)
    acts = memory.annotate(acts, asin)   # 记忆护栏：历史否决 / 5天稳定期
    blocked = [a for a in acts if a.blocked]
    pending = [a for a in acts if not a.blocked]

    mode = "真实执行" if args.execute else "DRY-RUN（仅预览，不写）"
    print(f"== 审核制执行（{mode}）==")
    print(f"可执行 {len(pending)} 个，护栏拦截 {len(blocked)} 个。\n")
    if blocked:
        print("【护栏拦截，不会执行】")
        for a in blocked:
            print(f"  ✗ {a.summary()}  — {a.block_reason}")
        print()
    if args.execute and not args.from_mcp:
        print("真实执行需要 --from-mcp <服务器>（且该服务器配好 writeActions 映射）。", file=sys.stderr)
        return 2

    from . import permission
    state = permission.PermissionState()
    if args.yes:  # --yes：批准所有未被护栏拦截的（等于对每类都"本会话允许"）
        state.session_allow.update({"negative", "reduce_bid", "scale_up"})
    confirmed: list = []
    for a in pending:
        if not a.executable:
            print(f"● {a.summary()}（缺当前bid，仅建议，跳过执行）")
            print(f"    理由:{a.reason}")
            continue
        decision = permission.request(a, state)
        if decision == permission.ABORT:
            print("  已全部停止。")
            break
        if decision == permission.DENY:
            memory.record_decision(asin, a.search_term, a.kind, "reject")  # 记住否决
        if decision == permission.APPROVE:
            confirmed.append(a)
            memory.record_decision(asin, a.search_term, a.kind, "approve")

    print(f"\n已确认 {len(confirmed)} 个，开始{'执行' if args.execute else '预演'}：")
    from .mcp_client import MCPError
    for a in confirmed:
        try:
            r = executor.execute(a, args.from_mcp or "", dry_run=not args.execute)
            print(f"  {'✓' if r['ok'] else '✗'} {r['detail']}")
        except MCPError as e:
            print(f"  ✗ {a.summary()} — {e}")
    if not args.execute:
        print("\n（这是 DRY-RUN。确认无误后加 --execute --from-mcp <服务器> 真实执行。）")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    from . import audit, executor
    if args.action == "list":
        rows = audit.load_all()
        if not rows:
            print("(暂无审计记录)")
            return 0
        for e in rows:
            src = e.get("source") or e.get("server") or ""
            print(f"  {e.get('id','?')}  {e.get('ts','')}  {e.get('kind','')}  "
                  f"{e.get('search_term','')}  [{src}]")
        return 0
    if args.action == "rollback":
        if not args.id:
            print("用法: ivyea audit rollback <审计ID>", file=sys.stderr)
            return 2
        entry = audit.get(args.id)
        if entry and entry.get("source") == "lingxing":
            from . import lingxing_write as lw
            r = lw.rollback(args.id)
        else:
            r = executor.rollback(args.id)
        print(("✓ " if r["ok"] else "✗ ") + r["detail"])
        return 0 if r["ok"] else 1
    return 2


_C = {"g": "\033[32m", "c": "\033[36m", "d": "\033[2m", "b": "\033[1m", "x": "\033[0m"}

_BANNER = r"""
 ___                          _                    _
|_ _|_   ___   _ ___  __ _   / \   __ _  ___ _ __ | |_
 | |\ \ / / | | / _ \/ _` | / _ \ / _` |/ _ \ '_ \| __|
 | | \ V /| |_| |  __/ (_| |/ ___ \ (_| |  __/ | | | |_
|___| \_/  \__, |\___|\__,_/_/   \_\__, |\___|_| |_|\__|
           |___/                   |___/"""

# (命令, 说明)
# 对齐 Claude Code / Hermes 的常用快捷指令集
SLASH_COMMANDS = [
    ("/help", "显示帮助与命令"),
    ("/model", "查看/切换主脑模型 (如 /model deepseek:deepseek-chat)"),
    ("/config", "打开配置向导"),
    ("/status", "查看当前配置与状态"),
    ("/mcp", "列出已配置的 MCP 服务器"),
    ("/tools", "列出 Agent 可用工具"),
    ("/knowledge", "搜索内置亚马逊知识库：/knowledge 否词"),
    ("/skill", "搜索可复用运营 Skill：/skill listing"),
    ("/workspace", "项目理解：/workspace map|search|explain"),
    ("/patch", "结构化补丁：/patch make|validate|apply|tests"),
    ("/gitops", "Git 工作流：/gitops status|diff|stage|commit|tag"),
    ("/memory", "记忆：状态/最近巡检；/memory <词> 检索"),
    ("/plan", "进入/退出计划模式（只读，不写入）"),
    ("/approve", "批准并退出计划模式，继续执行"),
    ("/cost", "本会话 token 用量与成本估算"),
    ("/compact", "压缩上下文；/compact auto on|off 控制自动压缩"),
    ("/init", "生成账户指令模板 AGENTS.md（长期打法/边界，自动注入）"),
    ("/raw", "切换 Markdown 渲染 / 原始流式输出"),
    ("/clear", "清空当前对话上下文"),
    ("/exit", "退出 (亦可 /quit)"),
]


class _LiveSpinner:
    """生成时的轻量转圈反馈（不打印 token；收尾渲染 markdown）。"""
    _F = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self):
        self.i = 0
        self.on = False

    def tick(self, _text: str = "") -> None:
        self.i += 1
        frame = self._F[self.i % len(self._F)]
        sys.stdout.write(f"\r{_C['c']}{frame}{_C['x']} {_C['d']}生成中…{_C['x']}")
        sys.stdout.flush()
        self.on = True

    def clear(self) -> None:
        if self.on:
            sys.stdout.write("\r" + " " * 20 + "\r")
            sys.stdout.flush()
            self.on = False


def _help_text() -> str:
    lines = [f"{_C['b']}斜杠命令{_C['x']}（输入 / 后按 Tab 可补全）："]
    for cmd, desc in SLASH_COMMANDS:
        lines.append(f"  {_C['c']}{cmd:<9}{_C['x']} {desc}")
    lines.append("")
    lines.append(f"{_C['b']}直接说人话就行{_C['x']}，例如：")
    lines.append(f"  {_C['d']}· 看下 B0XXXXXXXX 这周广告，数据用 sample CSV{_C['x']}")
    lines.append(f"  {_C['d']}· 帮我分析这份搜索词报告 /path/report.csv，asin B0...{_C['x']}")
    lines.append(f"{_C['d']}写操作会逐条弹人工审批，未确认不会执行。{_C['x']}")
    return "\n".join(lines)


def _run_embedded_cli(line: str) -> int:
    try:
        argv = shlex.split(line[1:])
    except ValueError as e:
        print(ui.message("warn", f"命令解析失败：{e}"))
        return 2
    if not argv:
        return 2
    return main(argv)


def _setup_readline() -> None:
    """斜杠命令 Tab 补全（stdlib readline；Windows 无则静默跳过）。"""
    try:
        import readline
    except Exception:
        return
    cmds = [c for c, _ in SLASH_COMMANDS] + ["/quit"]

    def completer(text, state):
        if not text.startswith("/"):
            return None
        opts = [c + " " for c in cmds if c.startswith(text)]
        return opts[state] if state < len(opts) else None

    readline.set_completer(completer)
    readline.parse_and_bind("tab: complete")
    try:
        readline.set_completer_delims(" ")
    except Exception:
        pass


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _print_welcome_box(lines: list, width: int = 58) -> None:
    """Claude Code 风格圆角欢迎框（按显示宽度对齐中英文混排）。"""
    try:
        from prompt_toolkit.utils import get_cwidth
    except Exception:
        def get_cwidth(ch): return 1
    inner, cy, x = width - 2, _C["c"], _C["x"]
    print(f"{cy}╭{'─' * inner}╮{x}")
    for ln in lines:
        w = sum(get_cwidth(ch) for ch in _strip_ansi(ln))
        print(f"{cy}│{x} {ln}{' ' * max(0, inner - 1 - w)}{cy}│{x}")
    print(f"{cy}╰{'─' * inner}╯{x}")


def _cmd_chat(args: argparse.Namespace) -> int:
    from . import agent_loop, agent_tools, config as cfg, pricing, sessions, context as ctx_mod, markdown, memory, profiles
    from .providers import from_settings, build_chain, LLMError

    # 首次运行：无配置 → 先走引导
    if not cfg.SETTINGS_FILE.exists() and not cfg.get_active_key() and sys.stdin.isatty():
        print(f"{_C['d']}（检测到首次运行，先带你配置）{_C['x']}")
        _cmd_onboard(args)
        print()

    def _label() -> str:
        s = cfg.load_settings()
        return s.get("label", s.get("provider", "deepseek"))

    ctx = agent_tools.ToolContext(
        from_mcp=args.from_mcp, execute=args.execute, workspace=os.getcwd(),
        protected=[w for w in (args.protected or "").split(",") if w.strip()],
        task_id=getattr(args, "task_id", "") or "")
    if getattr(args, "asin", None):
        ctx.asin = args.asin
    meter = pricing.UsageMeter()
    _ui = {"ctx": 0}                                        # 状态栏:上下文 token 估算
    instructions = memory.load_instructions(os.getcwd())   # USER.md/AGENTS.md 持久指令
    profile_key = getattr(args, "asin", None) or "default"
    profile_context = profiles.context_text(profiles.resolve(asin=getattr(args, "asin", "") or ""), label=profile_key)

    def _sys_msg() -> dict:
        content = agent_loop.SYSTEM_PROMPT + (agent_loop.PLAN_NOTE if ctx.plan_mode else "")
        if profile_context:
            content += "\n\n" + profile_context
        if instructions:
            content += "\n\n[长期指令/画像]\n" + instructions
        return {"role": "system", "content": content}

    # ── resume / continue ─────────────────────────────────────────────
    sid = None
    messages = [_sys_msg()]
    resume_target = getattr(args, "resume", None)
    if getattr(args, "cont", False) and not resume_target:
        resume_target = sessions.latest_id() or None
    if resume_target:
        rid = resume_target if resume_target is not True else sessions.latest_id()
        sess = sessions.load(rid) if rid else None
        if sess and sess.get("messages"):
            messages = sess["messages"]
            sid = sess["id"]
            u = sess.get("usage") or {}
            meter.cost = u.get("cost", 0.0); meter.turns = u.get("turns", 0)
            meter.prompt = u.get("prompt", 0); meter.completion = u.get("completion", 0)
            print(f"{_C['d']}（已续接会话 {sid}，{meter.turns} 轮历史）{_C['x']}")
        else:
            print(f"{_C['d']}（未找到可续接的会话，开新会话）{_C['x']}")
    if sid is None:
        sid = sessions.new_id()
    ctx.session_id = sid
    render_md = not getattr(args, "raw", False)   # 默认 markdown 渲染

    def _persist():
        try:
            sessions.save(sid, messages, model=cfg.get_model_config().get("model", ""),
                          usage={"cost": meter.cost, "turns": meter.turns,
                                 "prompt": meter.prompt, "completion": meter.completion})
        except Exception:
            pass

    keyst = _model_key_label(cfg.load_settings())
    mode = "真实写" if args.execute else "dry-run"
    print(f"{_C['c']}{_C['b']}{_BANNER}{_C['x']}")
    _print_welcome_box([
        f"{_C['c']}✻{_C['x']} {_C['b']}亚马逊运营 Agent{_C['x']} · 规则引擎+LLM复核+审核制执行 · 自托管",
        f"{_C['d']}主脑 {_label()}（{keyst}）· 执行 {mode}{_C['x']}",
        f"{_C['d']}/help 看命令 · 直接说需求 · /exit 退出{_C['x']}",
    ])
    print()

    from . import chat_input

    def _status() -> str:
        plan = "计划模式 · " if ctx.plan_mode else ""
        cost = f"¥{meter.cost:.4f} · " if meter.turns else ""
        cx = f"ctx ~{_ui['ctx'] // 1000}k · " if _ui["ctx"] else ""
        return (f" ivyea · {_label()} · {plan}"
                f"{'真实写' if args.execute else 'dry-run'} · {cx}{cost}/help 命令、Tab 补全 ")

    ci = chat_input.ChatInput(SLASH_COMMANDS, _status)

    while True:
        line = ci.read("❯ ")
        if line is chat_input.EXIT:
            print("\n再见。")
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit"):
            print("再见。")
            return 0
        if line in ("/help", "/", "/?"):
            print(_help_text()); continue
        if line == "/clear":
            messages = [_sys_msg()]
            print(ui.message("success", "已清空对话上下文")); continue
        if line == "/plan":
            ctx.plan_mode = not ctx.plan_mode
            messages[0] = _sys_msg()
            print(ui.message("info", "已进入计划模式（只读，不写入；/approve 批准后执行）。") if ctx.plan_mode
                  else ui.message("success", "已退出计划模式。")); continue
        if line == "/approve":
            if ctx.plan_mode:
                ctx.plan_mode = False
                messages[0] = _sys_msg()
                print(ui.message("success", "已批准，退出计划模式。说“继续/执行”让我落地计划。"))
            else:
                print(ui.message("warn", "当前不在计划模式。"))
            continue
        if line == "/cost":
            lim = pricing.daily_limit()
            tail = f" · 今日 ¥{pricing.today_spend():.4f}" + (f"/¥{lim:.2f} 上限" if lim > 0 else "（无上限，可 config set daily_cost_limit_cny）")
            print((meter.summary() if meter.turns else "本会话还没有模型调用。") + tail); continue
        if line == "/raw":
            render_md = not render_md
            print(ui.message("success", f"已切换为 {'原始流式' if not render_md else 'Markdown 渲染'} 输出。")); continue
        if line == "/compact":
            ak = cfg.get_active_key()
            if not ak:
                print(ui.message("warn", "未配 key，无法压缩。")); continue
            before = sum(len(str(m.get('content') or '')) for m in messages)
            provider = from_settings(cfg.get_model_config(), ak)
            messages, summary = ctx_mod.compact(messages, provider)
            after = sum(len(str(m.get('content') or '')) for m in messages)
            if summary:
                memory.remember_summary(summary, sid)
            _persist()
            print(ui.message("success", f"已压缩上下文（约 {before}→{after} 字），摘要已入库。") if summary
                  else ui.message("info", "上下文较短，无需压缩。"))
            continue
        if line in ("/compact auto", "/compact auto status"):
            state = "开启" if bool(cfg.get_setting("auto_compact", False)) else "关闭"
            th = int(cfg.get_setting("compact_at_tokens", ctx_mod.DEFAULT_COMPACT_AT))
            print(ui.message("info", f"自动压缩：{state} · 阈值 {th} prompt tokens"))
            continue
        if line in ("/compact auto on", "/compact auto off"):
            enabled = line.endswith(" on")
            cfg.set_setting("auto_compact", enabled)
            print(ui.message("success", f"自动压缩已{'开启' if enabled else '关闭'}。手动压缩仍可用 /compact。"))
            continue
        if line == "/init":
            p = memory.init_agents(str(cfg.IVYEA_DIR / "AGENTS.md"))
            if p[0]:
                print(ui.message("success", f"已生成账户指令模板：{p[1]}"))
                print(ui.message("info", "填好后重开对话即自动注入。"))
            else:
                print(ui.message("info", f"已存在：{p[1]}（未覆盖）。`ivyea config edit` 或直接编辑它。"))
            instructions = memory.load_instructions(os.getcwd())
            messages[0] = _sys_msg()
            continue
        if line == "/mcp":
            servers = cfg.load_mcp().get("mcpServers", {})
            print("MCP 服务器: " + (", ".join(servers) if servers else "(无，ivyea mcp add)")); continue
        if line == "/knowledge" or line.startswith("/knowledge "):
            from . import knowledge
            q = line[10:].strip() if line.startswith("/knowledge ") else ""
            if not q:
                print("用法：/knowledge <关键词>，例如 /knowledge 否词")
            else:
                print(knowledge.render_search(q, limit=5))
            continue
        if line == "/skill" or line.startswith("/skill "):
            from . import skills
            q = line[7:].strip() if line.startswith("/skill ") else ""
            if not q:
                print(skills.render_list())
            else:
                print(skills.render_search(q, limit=8))
            continue
        if (
            line == "/workspace" or line.startswith("/workspace ")
            or line == "/patch" or line.startswith("/patch ")
            or line == "/gitops" or line.startswith("/gitops ")
        ):
            _run_embedded_cli(line)
            continue
        if line == "/tools":
            for t in agent_tools.TOOL_SCHEMAS:
                f = t["function"]; print(f"  {f['name']} — {f['description']}")
            continue
        if line == "/memory" or line.startswith("/memory "):
            from . import memory
            q = line[7:].strip() if line.startswith("/memory ") else ""
            if q:
                hits = memory.search(q, limit=10)
                print("\n".join(f"  · {h['text']}" for h in hits) or "（无匹配记忆）")
            else:
                st = memory.stats()
                print(f"记忆：决策 {st['decisions']}（批准{st['approved']}/否决{st['rejected']}）· "
                      f"巡检 {st['runs']} 次 · FTS5={'on' if st['fts'] else 'off(LIKE)'}")
                for r in memory.recent_runs(limit=5):
                    import time as _t
                    print(f"  · {_t.strftime('%m-%d %H:%M', _t.localtime(r['ts']))} {r['asin']} "
                          f"否{r['negatives']}/放{r['scale']}/降{r['reduce']}")
                print(f"  {_C['d']}/memory <关键词> 检索；对话里也可让我 记住/回忆{_C['x']}")
            continue
        if line == "/status":
            _print_config(); continue
        if line in ("/config", "/model"):
            _model_picker() if line == "/model" else _config_wizard()
            continue
        if line.startswith("/model "):  # /model <id> 直接切
            mid = line.split(None, 1)[1].strip()
            m = __import__("ivyea_agent.models", fromlist=["by_id"]).by_id(mid)
            if m:
                cfg.apply_model(m)
                print(f"已切换主脑: {m['label']}（{'已配 key' if cfg.get_active_key() else '未配 key，用 /model 配置'}）")
            else:
                print(ui.message("warn", f"未知模型 id：{mid}。用 /model 看清单。"))
            continue
        if line.startswith("/"):
            hits = [c for c, _ in SLASH_COMMANDS if c.startswith(line.split()[0])]
            tip = ("，你是否想用：" + " ".join(hits)) if hits else "，输入 /help 看全部"
            print(ui.message("warn", f"未知命令 {line.split()[0]}{tip}")); continue

        # 自然语言 → Agent 循环
        api_key = cfg.get_active_key()
        current_model_settings = cfg.load_settings()
        if (current_model_settings.get("kind") in ("native", "oauth", "login")
                and current_model_settings.get("api_mode") not in ("gemini_native", "gemini_code_assist", "bedrock_converse", "copilot_chat_completions", "codex_responses")):
            print(ui.message("warn", "当前 provider 需要尚未接入的原生/OAuth transport。请用 /model 切到 API key、OpenAI 兼容、本地或自定义 provider。"))
            continue
        if _model_needs_key(current_model_settings) and not api_key:
            print(ui.message("warn", f"未配置主脑模型 key（{current_model_settings.get('key_env')}）。用 /model 配 key，或切到本地/自定义 no-key provider。"))
            continue
        # 成本护栏：每日 ¥ 上限
        _lim = pricing.daily_limit()
        if _lim > 0 and pricing.today_spend() >= _lim:
            if not _ask(f"今日已花 ¥{pricing.today_spend():.2f} 达上限 ¥{_lim:.2f}，仍继续？(y/N)").strip().lower().startswith("y"):
                print(ui.message("info", "已暂停。调整上限：ivyea config set daily_cost_limit_cny <元>"))
                continue
        from . import engineering_context, knowledge, skills
        kctx, kids = knowledge.context_for_query(line, limit=3)
        sctx, sids = skills.context_for_query(line, limit=2)
        ectx = engineering_context.build(os.getcwd(), line)
        user_content = line
        if ectx:
            user_content += "\n\n[工程上下文]\n" + ectx
            print(ui.stage("Code", "计划 → 读上下文 → 修改/生成补丁 → 测试 → 复查"))
            print(ui.message("muted", "已注入工程上下文"))
        if sctx:
            user_content += ("\n\n[Ivyea Skill：本轮相关可复用流程]\n"
                             + sctx
                             + "\n\n要求：优先按 skill workflow 组织执行步骤；涉及事实依据时再结合知识库。")
            print(ui.message("muted", "已注入 skill: " + ", ".join(sids)))
        if kctx:
            user_content += ("\n\n[Ivyea 内置亚马逊知识库：本轮相关摘录]\n"
                             + kctx
                             + "\n\n要求：使用这些知识时说明依据，若与用户账户记忆冲突，以用户账户记忆为准。")
            print(ui.message("muted", "已注入知识卡: " + ", ".join(kids)))
        import time as _turn_time
        ctx.turn_id = _turn_time.strftime("%Y%m%d-%H%M%S")
        messages.append({"role": "user", "content": user_content})
        try:
            mcfg = cfg.get_model_config()
            provider = build_chain(mcfg, api_key, narrate=lambda s: print(s))
            if render_md:
                # 缓冲 + spinner，收尾渲染 markdown
                spin = _LiveSpinner()
                out = agent_loop.run_turn_stream(
                    provider, ctx, messages, model=mcfg.get("model", ""),
                    render=spin.tick, narrate=lambda s: (spin.clear(), print(s)))
                spin.clear()
                print(f"{_C['c']}●{_C['x']} " + markdown.render(out["text"]))
            else:
                print(f"{_C['c']}●{_C['x']} ", end="", flush=True)
                out = agent_loop.run_turn_stream(provider, ctx, messages, model=mcfg.get("model", ""))
            c = meter.add(mcfg.get("model", ""), out.get("usage") or {})
            _ui["ctx"] = int((out.get("usage") or {}).get("prompt_tokens") or _ui["ctx"])
            if c:
                day = pricing.add_spend(c)
                print(ui.message("muted", f"本轮 ¥{c:.4f} · 累计 ¥{meter.cost:.4f} · 今日 ¥{day:.4f}"))
            from . import panels
            if ctx.todos:
                print(panels.render_todos(ctx.todos, color=sys.stdout.isatty()))
            print()
            # 记忆：会话转录入库 + 自策展提示
            memory.index_turn("user", line, sid)
            memory.index_turn("assistant", out.get("text", ""), sid)
            hint = memory.nudge_hint(out.get("text", ""))
            if hint:
                print(f"{_C['d']}  💡 {hint}{_C['x']}")
            # 自动压缩默认关闭；长上下文只提醒，避免完整任务中途被压缩打断。
            if ctx_mod.should_compact(int((out.get('usage') or {}).get('prompt_tokens') or 0)):
                messages, _s = ctx_mod.compact(messages, provider)
                if _s:
                    memory.remember_summary(_s, sid)
                    print(ui.message("info", "上下文较长，已自动压缩并入库摘要以省 token"))
            elif ctx_mod.should_warn_compact(int((out.get('usage') or {}).get('prompt_tokens') or 0)):
                print(ui.message("muted", "上下文已经较长；需要节省 token 时手动执行 /compact，或 /compact auto on 开启自动压缩。"))
            _persist()
        except KeyboardInterrupt:
            print("\n" + ui.message("info", "已中断本轮，会话保留。继续输入即可。"))
        except LLMError as e:
            print("\n" + ui.message("error", f"模型错误: {e}"))
            messages.pop()  # 撤回这条 user，避免污染上下文


def _cmd_model(args: argparse.Namespace) -> int:
    from . import config as cfg, models
    cfg.ensure_dirs()
    if args.spec in ("auth", "login"):
        return _cmd_model_auth(args, "auth")
    if args.spec in ("logout", "disconnect"):
        return _cmd_model_auth(args, "logout")
    if args.spec in ("providers", "provider"):
        print(_render_model_providers())
        return 0
    if args.spec in ("doctor", "status"):
        print(_render_model_doctor())
        return 0
    if args.spec == "list":
        for group, items in models.grouped():
            print(group)
            for m in items:
                status = "" if m.get("status") == "usable" else f" [{m.get('auth_type', '-')}:待接]"
                print(f"  {m['id']:<34} {m['label']}{status}")
        return 0
    if args.spec:  # ivyea model <id>
        m = models.by_id(args.spec)
        if not m:
            print(f"未知模型 id：{args.spec}。`ivyea model list` 看清单，或 `ivyea model` 交互选。",
                  file=sys.stderr)
            return 2
        cfg.apply_model(m)
        print(f"已切换主脑: {m['label']}"
              f"（{_model_key_label(cfg.load_settings())}）")
        return 0
    _model_picker()   # 无参 → 交互选择清单
    return 0


def _cmd_memory(args: argparse.Namespace) -> int:
    from . import memory
    import time as _t
    if args.action == "search":
        if not args.query:
            print("用法: ivyea memory search <关键词>", file=sys.stderr); return 2
        hits = memory.search(args.query, limit=15)
        print("\n".join(f"  · {_t.strftime('%Y-%m-%d', _t.localtime(h['ts']))} {h['text']}" for h in hits)
              or "（无匹配）")
        return 0
    if args.action == "note":
        print(memory.read_note(args.query or "") or "（暂无记忆笔记）"); return 0
    # 默认 status
    st = memory.stats()
    print(f"记忆库: {st['db']}")
    print(f"决策 {st['decisions']}（批准 {st['approved']} / 否决 {st['rejected']}）· "
          f"巡检 {st['runs']} 次 · 全文检索 FTS5={'on' if st['fts'] else 'off(LIKE 兜底)'}")
    print("最近巡检：")
    for r in memory.recent_runs(limit=8):
        print(f"  · {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(r['ts']))} {r['asin'] or '-'} "
              f"否{r['negatives']}/放{r['scale']}/降{r['reduce']}")
    return 0


def _cmd_knowledge(args: argparse.Namespace) -> int:
    from . import knowledge

    if args.action == "list":
        for card in knowledge.list_cards():
            tags = ",".join(card.get("tags", [])[:4])
            conf = card.get("confidence", "unknown")
            print(f"{card['id']:<44} {card['source_type']:<34} {conf:<11} {card['title']}  [{tags}]")
        return 0
    if args.action == "audit":
        print(knowledge.render_audit())
        return 0
    if args.action == "rebuild":
        res = knowledge.rebuild()
        print(f"已重建用户知识索引：{res['user_cards']} 张用户知识卡；清理缺失 {len(res['missing_pruned'])} 张")
        print(f"sources: {res['sources']}")
        print(f"index: {res['index']['db']} cards={res['index']['cards']} fts={'on' if res['index']['fts'] else 'off'}")
        return 0
    if args.action == "index":
        res = knowledge.rebuild_index()
        print(f"已重建知识索引：{res['cards']} 张卡 · FTS5={'on' if res['fts'] else 'off'} · {res['db']}")
        return 0
    if args.action == "conflicts":
        print(knowledge.render_conflicts())
        return 0
    if args.action == "import":
        if not args.query:
            print("用法: ivyea knowledge import <本地Markdown/TXT路径>", file=sys.stderr)
            return 2
        tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
        card = knowledge.import_file(
            args.query,
            title=args.title or "",
            source_type=args.source_type or "user",
            confidence=args.confidence or "",
            tags=tags,
            card_id=args.id or "",
            license=args.license or "user_supplied",
        )
        print(f"已导入知识卡：{card['id']} -> {card['path']}")
        return 0
    if args.action == "url":
        if not args.query:
            print("用法: ivyea knowledge url <https://...>", file=sys.stderr)
            return 2
        tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
        card = knowledge.import_url(
            args.query,
            title=args.title or "",
            source_type=args.source_type or "user",
            confidence=args.confidence or "",
            tags=tags,
            card_id=args.id or "",
            license=args.license or "user_supplied",
        )
        print(f"已导入知识卡：{card['id']} -> {card['path']}")
        return 0
    if args.action == "show":
        if not args.query:
            print("用法: ivyea knowledge show <知识ID>", file=sys.stderr)
            return 2
        card = knowledge.get_card(args.query)
        if not card:
            print(f"未找到知识ID：{args.query}", file=sys.stderr)
            return 1
        print(card["body"])
        return 0
    if args.action == "search":
        if not args.query:
            print("用法: ivyea knowledge search <关键词>", file=sys.stderr)
            return 2
        print(knowledge.render_search(args.query, limit=args.limit))
        return 0
    return 2


def _cmd_skill(args: argparse.Namespace) -> int:
    from . import skills

    if args.action == "list":
        print(skills.render_list())
        return 0
    if args.action == "audit":
        print(skills.render_audit())
        return 0
    if args.action == "status":
        print(skills.render_status())
        return 0
    if args.action == "export-lock":
        path = skills.write_lockfile(args.output)
        print(f"已写入 skill lockfile：{path}")
        return 0
    if args.action == "create":
        if not args.query:
            print("用法: ivyea skill create <skill_id> --title ...", file=sys.stderr)
            return 2
        body = args.body or ""
        if args.body_file:
            body = Path(args.body_file).expanduser().read_text(encoding="utf-8")
        sk = skills.create_user_skill(
            args.query,
            title=args.title or "",
            domain=args.domain or "",
            description=args.description or "",
            triggers=args.trigger or [],
            tools=args.tool or [],
            knowledge_ids=args.knowledge or [],
            body=body,
            overwrite=args.force,
        )
        print(f"已创建 skill：{sk.id} -> {sk.path}")
        return 0
    if args.action == "search":
        if not args.query:
            print("用法: ivyea skill search <关键词>", file=sys.stderr)
            return 2
        print(skills.render_search(args.query, limit=args.limit))
        return 0
    if args.action in ("show", "run"):
        if not args.query:
            print(f"用法: ivyea skill {args.action} <skill_id>", file=sys.stderr)
            return 2
        sk = skills.get_skill(args.query)
        if not sk:
            print(f"未找到 skill：{args.query}", file=sys.stderr)
            return 1
        print(skills.render_skill(sk, include_knowledge=True))
        return 0
    return 2


def _cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor
    checks = doctor.run_checks()
    print(doctor.render(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


def _cmd_self(args: argparse.Namespace) -> int:
    from . import permission, self_manage

    if args.action == "status":
        print(self_manage.render_status())
        return 0
    if args.action == "doctor":
        print(self_manage.render_doctor(self_manage.install_doctor()))
        return 0
    if args.action == "backup":
        path = self_manage.backup(args.output)
        print(f"已写入备份：{path}")
        return 0
    if args.action in ("upgrade", "uninstall"):
        if args.action == "upgrade":
            plan = self_manage.upgrade_plan(version=args.version or "latest", ref=args.ref or "", method=args.method or "")
        else:
            plan = self_manage.uninstall_plan(keep_data=not args.remove_data, method=args.method or "")
        preview = self_manage.render_plan(plan)
        if not args.execute:
            print(preview)
            print("\n（这是 dry-run。确认后加 --execute 真实执行。）")
            return 0
        if not args.yes:
            state = permission.PermissionState()
            decision = permission.request_intent({"op_type": f"self.{args.action}"}, preview, state)
            if decision != permission.APPROVE:
                print("已取消。")
                return 1
        print(self_manage.render_execution(self_manage.execute_plan(plan, timeout=args.timeout)))
        return 0
    return 2


def _cmd_action(args: argparse.Namespace) -> int:
    from . import action_queue, actions as act_mod, executor, guardrails, memory, profiles
    from .mcp_client import MCPError
    from pathlib import Path

    item_id = args.id or args.source or ""
    if args.action == "list":
        if args.summary:
            s = action_queue.summary()
            print(f"pending={s.get('pending', 0)} approved={s.get('approved', 0)} "
                  f"denied={s.get('denied', 0)} done={s.get('done', 0)} blocked={s.get('blocked', 0)}")
        print(action_queue.render(action_queue.list_items(status=args.status or "", limit=args.limit)))
        return 0
    if args.action == "report":
        items = action_queue.list_items(status=args.status or "", limit=args.limit)
        text = action_queue.render_report(items)
        if args.output:
            from pathlib import Path
            p = Path(args.output)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            print(f"已导出报告：{p}")
        else:
            print(text)
        return 0
    if args.action == "show":
        item = action_queue.get(item_id)
        if not item:
            print(f"未找到动作：{item_id}", file=sys.stderr)
            return 1
        print(__import__("json").dumps(item, ensure_ascii=False, indent=2))
        return 0
    if args.action == "clear":
        n = action_queue.clear(args.status or "")
        print(f"已清理 {n} 条。")
        return 0
    if args.action in ("approve", "deny", "done", "pending"):
        status = {"approve": "approved", "deny": "denied"}.get(args.action, args.action)
        if args.all:
            n = action_queue.set_many_status(
                status,
                from_status=args.status or "pending",
                limit=args.limit,
                include_blocked=args.include_blocked,
            )
            print(f"已批量更新 {n} 条为 {status}。")
            return 0
        if not item_id:
            print(f"用法: ivyea action {args.action} <ID>", file=sys.stderr)
            return 2
        ok = action_queue.set_status(item_id, status)
        print("已更新。" if ok else f"未找到动作：{item_id}")
        return 0 if ok else 1
    if args.action == "execute":
        if args.execute and not args.from_mcp:
            print("真实执行需要 --from-mcp <服务器>（且该服务器配好 writeActions 映射）。", file=sys.stderr)
            return 2
        if item_id:
            item = action_queue.get(item_id)
            if not item:
                print(f"未找到动作：{item_id}", file=sys.stderr)
                return 1
            items = [item]
        else:
            status = args.status or "approved"
            items = action_queue.list_items(status=status, limit=args.limit)
        if not items:
            print("没有可执行的队列项。先用 `ivyea action approve <ID>` 批准动作。")
            return 0

        dry_run = not args.execute
        print(f"== 动作队列{'真实执行' if args.execute else 'DRY-RUN 预演'} ==")
        ok_count = 0
        fail_count = 0
        skip_count = 0
        for item in items:
            iid = item.get("id", "")
            if item.get("status") != "approved":
                print(f"  - {iid} 跳过：状态为 {item.get('status')}，需先 approve。")
                skip_count += 1
                continue
            if item.get("blocked"):
                print(f"  - {iid} 跳过：护栏拦截，{item.get('block_reason') or '无原因'}")
                skip_count += 1
                continue
            action = action_queue.to_action(item)
            if not action.executable:
                print(f"  - {iid} 跳过：动作不可执行，{action.summary()}")
                skip_count += 1
                continue
            try:
                result = executor.execute(action, args.from_mcp or "", dry_run=dry_run)
            except MCPError as exc:
                result = {"ok": False, "detail": str(exc)}
            mark = "✓" if result.get("ok") else "✗"
            print(f"  {mark} {iid} {result.get('detail', action.summary())}")
            if result.get("ok"):
                ok_count += 1
                if not dry_run:
                    action_queue.mark_done(iid, str(result.get("detail", "")))
            else:
                fail_count += 1
        if dry_run:
            print("（这是 DRY-RUN。确认无误后加 --execute --from-mcp <服务器> 真实执行。）")
        print(f"完成：成功 {ok_count}，跳过 {skip_count}，失败 {fail_count}。")
        return 0 if fail_count == 0 else 1
    if args.action == "import":
        if not args.source:
            print("用法: ivyea action import <巡检输出目录或明细CSV>", file=sys.stderr)
            return 2
        detail = args.source
        asin = args.asin or ""
        if Path(args.source).is_dir():
            detail = act_mod.load_detail_from_dir(args.source) or ""
            asin = asin or act_mod.asin_from_dir(args.source)
        if not detail or not Path(detail).exists():
            print(f"找不到巡检明细 CSV：{args.source}", file=sys.stderr)
            return 2
        profile = profiles.resolve(asin=asin)
        protected = [w for w in (args.protected or "").split(",") if w.strip()]
        protected += list(profile.get("protected_terms") or [])
        acts = guardrails.annotate(act_mod.extract_actions(detail, asin=asin), protected_terms=protected)
        acts = memory.annotate(acts, asin)
        added = action_queue.enqueue_actions(acts, source=args.source, origin="import")
        print(f"已导入 {len(added)} 条新动作（重复 pending/approved 已跳过）。")
        if added:
            print(action_queue.render(added))
        return 0
    return 2


def _cmd_runs(args: argparse.Namespace) -> int:
    from . import memory, sessions
    import time as _t

    if args.kind in ("all", "patrol"):
        rows = memory.recent_runs(limit=args.limit)
        print("最近巡检：")
        if not rows:
            print("  （无）")
        for r in rows:
            print(f"  {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(r['ts']))} "
                  f"{r.get('asin') or '-'}  否{r.get('negatives', 0)}/放{r.get('scale', 0)}/降{r.get('reduce', 0)}")
    if args.kind in ("all", "sessions"):
        rows = sessions.listing(limit=args.limit)
        print("最近会话：")
        if not rows:
            print("  （无）")
        for r in rows:
            updated = _t.strftime('%Y-%m-%d %H:%M', _t.localtime(r["updated"])) if r.get("updated") else "-"
            print(f"  {updated}  {r['id']}  {r['turns']}轮  {r['preview']}")
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    from . import profiles

    key = args.key or "default"
    if args.action == "list":
        for name, profile in profiles.list_profiles():
            target = profile.get("target_acos")
            target_text = f"{target:.0%}" if target is not None else "未设"
            margin = profile.get("margin_rate")
            margin_text = f"{margin:.0%}" if margin is not None else "-"
            protected = ",".join(profile.get("protected_terms") or [])
            stage = profile.get("stage") or "-"
            risks = ",".join(profile.get("listing_risks") or [])
            print(f"{name:<16} site={profile.get('site', 'US'):<3} target={target_text:<5} margin={margin_text:<4} "
                  f"stage={stage} protected={protected or '-'} risks={risks or '-'}")
        return 0
    if args.action == "show":
        print(profiles.render(profiles.get(key), label=key))
        return 0
    if args.action == "set":
        fields = {
            "site": args.site,
            "target_acos": args.target_acos,
            "margin_rate": args.margin_rate,
            "breakeven_acos": args.breakeven_acos,
            "price": args.price,
            "currency": args.currency,
            "stage": args.stage,
            "protected_terms": args.protected,
            "core_terms": args.core,
            "competitor_terms": args.competitors,
            "listing_risks": args.listing_risks,
            "notes": args.notes,
        }
        profile = profiles.update(key, **fields)
        print("已保存画像：")
        print(profiles.render(profile, label=key))
        return 0
    return 2


def _cmd_scorecard(args: argparse.Namespace) -> int:
    from . import scorecard
    text = scorecard.render_md(scorecard.build(limit=args.limit))
    if args.output:
        from pathlib import Path
        p = Path(args.output)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已导出 Scorecard：{p}")
    else:
        print(text)
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    from . import traces
    if args.action == "stats":
        st = traces.stats(limit=args.limit)
        print(f"trace db: {st['db']}")
        print(f"events {st['events']} · tool_calls {st['tool_calls']} · failures {st['failures']} · avg_tool_ms {st['avg_tool_ms']}")
        return 0
    print(traces.render_recent(limit=args.limit, session_id=args.session or ""))
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from . import evals
    result = evals.run()
    print(evals.render(result))
    return 0 if result["ok"] else 1


def _read_optional_text(path: str = "", text: str = "") -> str:
    if text:
        return text
    if not path:
        return ""
    return Path(path).expanduser().read_text(encoding="utf-8", errors="replace")


def _cmd_listing(args: argparse.Namespace) -> int:
    from . import listing_audit

    if args.action != "audit":
        return 2
    search_terms = [s.strip() for s in (args.search_terms or "").split(",") if s.strip()]
    result = listing_audit.audit(
        title=_read_optional_text(args.title_file or "", args.title or ""),
        bullets=_read_optional_text(args.bullets_file or "", args.bullets or ""),
        aplus=_read_optional_text(args.aplus_file or "", args.aplus or ""),
        search_terms=search_terms,
        reviews=_read_optional_text(args.reviews_file or "", args.reviews or ""),
        price=args.price,
        rating=args.rating,
        review_count=args.review_count,
    )
    print(listing_audit.render(result))
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    from . import review_audit

    if args.action != "audit":
        return 2
    result = review_audit.audit(
        reviews=_read_optional_text(args.reviews_file or "", args.reviews or ""),
        qa=_read_optional_text(args.qa_file or "", args.qa or ""),
        rating=args.rating,
        review_count=args.review_count,
        price=args.price,
        coupon=args.coupon or "",
        competitor_price=args.competitor_price,
    )
    print(review_audit.render(result))
    return 0


def _cmd_offer(args: argparse.Namespace) -> int:
    from . import offer_audit

    if args.action != "audit":
        return 2
    result = offer_audit.audit(
        price=args.price,
        competitor_price=args.competitor_price,
        margin_rate=args.margin_rate,
        target_acos=args.target_acos,
        inventory_days=args.inventory_days,
        coupon=args.coupon or "",
        spend=args.spend,
        sales=args.sales,
    )
    print(offer_audit.render(result))
    return 0


def _cmd_competitor(args: argparse.Namespace) -> int:
    from . import competitor_audit

    if args.action != "audit":
        return 2
    result = competitor_audit.audit(
        own_terms=args.own_terms or "",
        search_terms=args.search_terms or "",
        competitor_terms=args.competitor_terms or "",
        category_terms=args.category_terms or "",
        protected_terms=args.protected_terms or "",
    )
    print(competitor_audit.render(result))
    return 0


def _cmd_weekly(args: argparse.Namespace) -> int:
    from . import weekly_review

    if args.action != "review":
        return 2
    text = weekly_review.render(weekly_review.build(limit=args.limit))
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"已导出周期复盘：{out}")
    else:
        print(text)
    return 0


def _cmd_alert(args: argparse.Namespace) -> int:
    from . import alerts, notify
    rows = alerts.check(limit=args.limit)
    text = alerts.render(rows)
    print(text)
    if args.notify:
        result = notify.send(
            text,
            title=args.title or "Ivyea Alerts",
            channel=args.channel,
            webhook_url=args.webhook_url or "",
        )
        print(notify.render_result(result))
        if not result.get("ok"):
            return 1
    return 1 if any(a["severity"] == "fail" for a in rows) else 0


def _cmd_notify(args: argparse.Namespace) -> int:
    from . import notify

    message = args.message or "Ivyea Agent 通知测试。"
    result = notify.send(
        message,
        title=args.title or "Ivyea Agent",
        channel=args.channel,
        webhook_url=args.webhook_url or "",
    )
    print(notify.render_result(result))
    return 0 if result.get("ok") else 1


def _cmd_schedule(args: argparse.Namespace) -> int:
    from . import schedule
    if args.action == "list":
        print(schedule.render_jobs())
        return 0
    if args.action == "set":
        if not args.name or not args.task:
            print("用法: ivyea schedule set <名称> <alert|weekly|eval> --every-hours 24", file=sys.stderr)
            return 2
        try:
            task_args = {}
            if args.notify:
                task_args = {
                    "notify": True,
                    "channel": args.channel,
                    "webhook_url": args.webhook_url or "",
                    "title": args.title or "",
                    "limit": args.limit,
                }
            elif args.limit != 500:
                task_args = {"limit": args.limit}
            job = schedule.set_job(args.name, args.task, every_hours=args.every_hours, args=task_args)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"已保存计划：{job['name']} task={job['task']} every={job['every_hours']}h")
        return 0
    if args.action == "remove":
        if not args.name:
            print("用法: ivyea schedule remove <名称>", file=sys.stderr)
            return 2
        print("已删除" if schedule.remove_job(args.name) else "未找到")
        return 0
    if args.action == "run-due":
        rows = schedule.run_due()
        if not rows:
            print("无到期任务。")
            return 0
        ok = True
        for row in rows:
            ok = ok and row["ok"]
            print(f"## {row['job']} ({row['task']}) {'OK' if row['ok'] else 'FAIL'}")
            print(row["output"])
        return 0 if ok else 1
    if args.action == "run":
        task = args.task or args.name
        if not task:
            print("用法: ivyea schedule run <alert|weekly|eval>", file=sys.stderr)
            return 2
        task_args = {}
        if args.notify:
            task_args = {
                "notify": True,
                "channel": args.channel,
                "webhook_url": args.webhook_url or "",
                "title": args.title or "",
                "limit": args.limit,
            }
        elif args.limit != 500:
            task_args = {"limit": args.limit}
        ok, text = schedule.run_task(task, task_args)
        print(text)
        return 0 if ok else 1
    return 2


def _cmd_policy(args: argparse.Namespace) -> int:
    from . import policy
    if args.action == "show":
        print(policy.render())
        return 0
    if args.action == "init":
        created, path = policy.init(force=args.force)
        print(("已生成" if created else "已存在") + f" policy：{path}")
        return 0
    if args.action == "check-path":
        ok, msg = policy.check_path(args.value or "", args.op or "read")
        print("OK" if ok else msg)
        return 0 if ok else 1
    if args.action == "check-command":
        ok, msg = policy.check_command(args.value or "")
        print("OK" if ok else msg)
        return 0 if ok else 1
    if args.action == "explain-command":
        result = policy.assess_command(args.value or "")
        print(policy.render_command_assessment(args.value or ""))
        return 0 if result.get("ok") else 1
    return 2


def _cmd_workspace(args: argparse.Namespace) -> int:
    from . import workspace
    root = args.root or "."
    options = workspace.ScanOptions(
        max_files=args.max_files,
        max_bytes=args.max_bytes,
        include_hidden=args.include_hidden,
    )
    if args.action == "index":
        idx = workspace.build_index(root, options)
        path = workspace.save_index(idx)
        print(workspace.render_index(idx, path))
        return 0
    if args.action == "search":
        rows = workspace.search(args.query or "", root=root, limit=args.limit)
        print(workspace.render_search(rows, args.query or ""))
        return 0 if rows else 1
    if args.action == "map":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_map(workspace.project_map(root)))
        return 0
    if args.action == "graph":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_graph(workspace.dependency_graph(root, limit=args.limit)))
        return 0
    if args.action == "inspect":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_inspect(workspace.project_inspect(root)))
        return 0
    if args.action == "symbols":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_symbols(workspace.symbol_index(root, query=args.query or "", limit=args.limit)))
        return 0
    if args.action == "impact":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_impact(workspace.impact_analysis(args.target or args.query or "", root=root, limit=args.limit)))
        return 0
    if args.action == "explain":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_explain(workspace.explain(args.target or args.query or ".", root=root)))
        return 0
    return 2


def _cmd_task(args: argparse.Namespace) -> int:
    from . import task_runner
    try:
        if args.action == "create":
            steps = []
            if args.step:
                steps.extend(args.step)
            elif args.steps:
                steps.extend([s.strip() for s in args.steps.split("|") if s.strip()])
            task = task_runner.create(args.title or "", steps=steps, notes=args.notes or "", workspace=args.workspace or "")
            print(task_runner.render(task))
            return 0
        if args.action == "list":
            print(task_runner.render_list(task_runner.list_tasks(limit=args.limit, status=args.status or "")))
            return 0
        if args.action == "show":
            print(task_runner.render(task_runner.load(args.id)))
            return 0
        if args.action == "start":
            print(task_runner.render(task_runner.start_next(args.id, note=args.notes or "")))
            return 0
        if args.action == "step":
            print(task_runner.render(task_runner.update_step(args.id, args.index, args.status, note=args.notes or "")))
            return 0
        if args.action == "status":
            print(task_runner.render(task_runner.set_status(args.id, args.status, note=args.notes or "")))
            return 0
        if args.action == "log":
            print(task_runner.render(task_runner.append_log(args.id, args.notes or "")))
            return 0
        if args.action == "resume":
            print(task_runner.render_resume(task_runner.load(args.id)))
            return 0
    except Exception as e:  # noqa: BLE001
        print(f"task 失败：{e}", file=sys.stderr)
        return 1
    return 2


def _cmd_gitops(args: argparse.Namespace) -> int:
    from . import git_workflow, permission
    root = args.root or "."
    if args.action == "status":
        print(git_workflow.render_status(git_workflow.status(root)))
        return 0
    if args.action == "diff":
        print(git_workflow.render_diff(git_workflow.diff_summary(root, staged=args.staged)))
        return 0
    if args.action == "workflows":
        print(git_workflow.render_workflows(git_workflow.workflows(root)))
        return 0
    if args.action == "ci":
        result = git_workflow.ci_status(root, remote=args.remote, limit=args.limit, timeout=args.timeout)
        print(git_workflow.render_ci_status(result))
        return 0 if result.get("ok") else 1
    if args.action == "release-plan":
        plan = git_workflow.release_plan(args.version or "", root)
        print(git_workflow.render_release_plan(plan))
        return 0 if plan.get("ok") else 1
    if args.action in ("stage", "commit", "tag"):
        result = git_workflow.write_action(
            args.action,
            root=root,
            files=args.file or [],
            message=args.message or "",
            tag=args.tag or "",
            execute=False,
            timeout=args.timeout,
        )
        preview = git_workflow.render_write_action(result)
        if not result.get("ok"):
            print(preview)
            return 1
        if args.execute:
            if not args.yes:
                state = permission.PermissionState()
                intent = {"op_type": f"git.{args.action}"}
                decision = permission.request_intent(intent, preview, state)
                if decision != permission.APPROVE:
                    print("已取消。")
                    return 1
            result = git_workflow.write_action(
                args.action,
                root=root,
                files=args.file or [],
                message=args.message or "",
                tag=args.tag or "",
                execute=True,
                timeout=args.timeout,
            )
        print(git_workflow.render_write_action(result))
        return 0 if result.get("ok") else 1
    return 2


def _cmd_codereview(args: argparse.Namespace) -> int:
    from . import code_review
    result = code_review.review_diff(args.root or ".", staged=args.staged)
    print(code_review.render(result))
    if not result.get("ok"):
        return 1
    return 1 if any(f.get("severity") == "high" for f in result.get("findings", [])) else 0


def _cmd_patch(args: argparse.Namespace) -> int:
    from . import patcher, permission

    try:
        if args.action == "make":
            spec = patcher.make_spec(args.path or "", args.old or "", args.new or "")
            text = json.dumps(spec, ensure_ascii=False, indent=2)
            if args.output:
                out = Path(args.output).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text + "\n", encoding="utf-8")
                print(f"已写入 patch spec：{out}")
            else:
                print(text)
            return 0
        if args.action in ("validate", "apply"):
            spec = patcher.load_spec(args.spec)
            if args.action == "validate":
                result = patcher.validate_spec(spec, root=args.root or ".")
                print(patcher.render_validation(result))
                return 0 if result["ok"] else 1
            validation = patcher.validate_spec(spec, root=args.root or ".")
            if not validation["ok"]:
                print(patcher.render_apply({"ok": False, "applied": False, "validation": validation, "message": "patch 校验失败"}))
                return 1
            execute = bool(args.execute)
            if execute and not args.yes:
                preview = patcher.render_validation(validation)
                state = permission.PermissionState()
                decision = permission.request_intent({"op_type": "patch_apply"}, preview, state)
                execute = decision == permission.APPROVE
                if decision == permission.ABORT:
                    print("用户终止。")
                    return 1
            result = patcher.apply_spec(spec, root=args.root or ".", execute=execute)
            print(patcher.render_apply(result))
            return 0 if result["ok"] else 1
        if args.action == "tests":
            cmds = patcher.suggested_tests(args.root or ".")
            print(patcher.render_tests(cmds))
            return 0
        if args.action == "run-tests":
            cmd = args.test_command or "python -m pytest"
            result = patcher.run_test_command(cmd, root=args.root or ".", timeout=args.timeout)
            print(patcher.render_test_result(result))
            return 0 if result["ok"] else 1
    except Exception as e:  # noqa: BLE001
        print(f"patch 失败：{e}", file=sys.stderr)
        return 1
    return 2


def _cmd_code(args: argparse.Namespace) -> int:
    from . import code_agent

    try:
        root = args.root or "."
        if args.action == "plan":
            print(code_agent.render_plan(code_agent.task_plan(args.goal or "", root=root)))
            return 0
        if args.action == "context":
            print(code_agent.render_context(code_agent.context(args.goal or "", root=root, limit=args.limit)))
            return 0
        if args.action == "brief":
            print(code_agent.render_brief(code_agent.brief(args.goal or "", root=root, budget=args.budget)))
            return 0
        if args.action == "quality":
            print(code_agent.render_quality(code_agent.quality(root=root)))
            return 0
        if args.action == "diff-brief":
            print(code_agent.render_diff_brief(code_agent.diff_brief(root=root, staged=args.staged)))
            return 0
        if args.action == "release-check":
            print(code_agent.render_release_check(code_agent.release_check(root=root, version=args.version or "")))
            return 0
        if args.action == "refs":
            print(code_agent.render_refs(code_agent.refs(args.goal or args.target or "", root=root, limit=args.limit)))
            return 0
        if args.action == "rename-plan":
            print(code_agent.render_rename_plan(code_agent.rename_plan(args.goal or args.target or "", args.new_name or "", root=root, limit=args.limit)))
            return 0
        if args.action == "run":
            provider = None
            if args.llm_patch and args.call:
                from .providers import from_settings
                provider = from_settings(config.get_model_config(), config.get_active_key())
            result = code_agent.run_loop(
                args.goal or "",
                root=root,
                test_command=args.test_command or "",
                run_tests_enabled=bool(args.run_tests),
                max_rounds=args.max_rounds,
                persist=True,
                llm_patch=bool(args.llm_patch),
                patch_provider=provider,
                timeout=args.timeout,
            )
            print(code_agent.render_run(result))
            return 0 if not result.get("test_result") or result["test_result"].get("ok") else 1
        if args.action == "runs":
            print(code_agent.render_run_list(code_agent.list_runs(limit=args.limit)))
            return 0
        if args.action == "show":
            print(code_agent.render_run(code_agent.load_run(args.goal or "")))
            return 0
        if args.action == "sandbox":
            print(code_agent.render_sandbox_plan(code_agent.sandbox_plan(root=root, name=args.name or "")))
            return 0
        if args.action == "test":
            result = code_agent.run_tests(args.test_command or "python -m pytest", root=root, timeout=args.timeout)
            print(code_agent.render_test_result(result))
            return 0 if result.get("ok") else 1
        if args.action == "repair":
            if args.output_file:
                text = Path(args.output_file).expanduser().read_text(encoding="utf-8", errors="replace")
            else:
                text = args.text or sys.stdin.read()
            print(code_agent.render_repair(code_agent.repair_plan(text, root=root)))
            return 0
        if args.action == "impact":
            print(code_agent.render_impact(code_agent.impact(args.goal or args.target or "", root=root)))
            return 0
        if args.action == "patch":
            if args.llm:
                provider = None
                if args.call:
                    from .providers import from_settings
                    provider = from_settings(config.get_model_config(), config.get_active_key())
                result = code_agent.llm_patch_candidate(
                    args.goal or "",
                    root=root,
                    path=args.path or "",
                    provider=provider,
                    timeout=args.timeout,
                )
            else:
                result = code_agent.patch_candidate(
                    args.goal or "",
                    root=root,
                    path=args.path or "",
                    old=args.old or "",
                    new=args.new or "",
                )
            print(code_agent.render_patch_candidate(result))
            return 0 if result.get("status") != "invalid" else 1
        if args.action == "review":
            print(code_agent.render_review(code_agent.review_ready(root=root, staged=args.staged)))
            return 0
    except Exception as e:  # noqa: BLE001
        print(f"code 失败：{e}", file=sys.stderr)
        return 1
    return 2


def _cmd_image(args: argparse.Namespace) -> int:
    from . import image_audit, ocr, vision
    if args.action == "ocr":
        print(ocr.render(ocr.run(args.paths or [], lang=args.lang or "eng", recursive=not args.no_recursive)))
        return 0
    if args.action == "vision":
        pkg = vision.build(
            args.provider,
            args.paths or [],
            product_context=args.context or "",
            model=args.model or "",
            max_images=args.max_images,
        )
        text = vision.render_package(pkg, include_payload=args.payload)
        if args.output:
            out = vision.write_package(pkg, args.output)
            text += f"\n已导出视觉请求包：{out}\n"
        if args.call:
            result = vision.call(pkg, api_key=args.api_key or "", timeout=args.timeout)
            text += "\n" + vision.render_call(result)
            if not result.get("ok"):
                print(text)
                return 1
        print(text)
        return 0
    if args.action != "audit":
        return 2
    result = image_audit.audit(args.paths or [], recursive=not args.no_recursive)
    text = image_audit.render(result)
    if args.prompt:
        prompt = image_audit.multimodal_prompt(result, product_context=args.context or "")
        if args.prompt_out:
            out = Path(args.prompt_out).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(prompt, encoding="utf-8")
            text += f"\n已导出多模态审核 Prompt：{out}\n"
        else:
            text += "\n## 多模态审核 Prompt\n\n" + prompt + "\n"
    print(text)
    return 0


def _cmd_vision(args: argparse.Namespace) -> int:
    from . import vision
    pkg = vision.build_general(
        args.provider,
        args.paths or [],
        task=args.task or "",
        context=args.context or "",
        model=args.model or "",
        max_images=args.max_images,
    )
    text = vision.render_package(pkg, include_payload=args.payload)
    if args.output:
        out = vision.write_package(pkg, args.output)
        text += f"\n已导出视觉请求包：{out}\n"
    if args.call:
        result = vision.call(pkg, api_key=args.api_key or "", timeout=args.timeout)
        text += "\n" + vision.render_call(result)
        if not result.get("ok"):
            print(text)
            return 1
    print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ivyea", description="Ivyea Agent — 亚马逊运营 CLI Agent")
    p.add_argument("--version", action="version", version=f"ivyea-agent {__version__}")
    # 顶层便捷标志：裸 ivyea 进对话时也能用（见 main 转发）
    p.add_argument("--resume", nargs="?", const=True, help="裸 ivyea：续接会话（留空=最近）")
    p.add_argument("--continue", dest="cont", action="store_true", help="裸 ivyea：续接最近会话")
    p.add_argument("--raw", action="store_true", help="裸 ivyea：原始流式输出")
    sub = p.add_subparsers(dest="command")  # 无子命令 → 默认进对话模式(见 main)

    pc = sub.add_parser("config", help="配置向导（无参=交互式）/ show / set / edit")
    pc.add_argument("action", nargs="?", choices=["show", "set", "edit"], default=None)
    pc.add_argument("key", nargs="?")
    pc.add_argument("value", nargs="?")
    pc.set_defaults(func=_cmd_config)

    pm = sub.add_parser("mcp", help="MCP 配置/自检（add/list/remove/edit/tools/call/suggest/template/validate/doctor）")
    pm.add_argument("action", choices=["add", "list", "remove", "edit", "tools", "call", "suggest", "template", "validate", "doctor"])
    pm.add_argument("name", nargs="?", help="服务器名（remove/tools/call 需要）")
    pm.add_argument("tool", nargs="?", help="工具名（call 需要）")
    pm.add_argument("--args", help="call 的入参 JSON，如 '{\"asin\":\"B0..\"}'")
    pm.set_defaults(func=_cmd_mcp)

    pp = sub.add_parser("patrol", help="只读广告巡检（CSV / --from-lingxing 店铺维度 / --from-mcp 通用源）")
    pp.add_argument("csv", nargs="?", help="搜索词报告路径 (csv/xlsx)；用 --from-mcp 时可省略")
    pp.add_argument("--from-mcp", dest="from_mcp", help="改用已配置的 MCP 服务器拉广告数据（需该服务器配好 dataSource 映射）")
    pp.add_argument("--from-lingxing", dest="from_lingxing", action="store_true",
                    help="走领星 OpenAPI 的店铺(sid)维度规则引擎巡检（需 --sid，先 ivyea lingxing setup）")
    pp.add_argument("--sid", help="领星店铺 SID（--from-lingxing 时必填，用 ivyea lingxing sellers 查）")
    pp.add_argument("--days", type=int, default=30, help="拉取天数，默认 30")
    pp.add_argument("--asin", help="指定分析的 ASIN（--from-mcp 时必填）")
    pp.add_argument("--site", help="站点代码，默认取配置/US")
    pp.add_argument("--target-acos", type=float, dest="target_acos", help="目标 ACoS，如 0.3")
    pp.add_argument("--report-type", dest="report_type", help="SP/SB/SD")
    pp.add_argument("--output-dir", dest="output_dir", help="输出目录")
    pp.add_argument("--no-llm", action="store_true", help="只跑规则引擎，跳过 AI 复核")
    pp.add_argument("--execute", action="store_true",
                    help="（仅 --from-lingxing）巡检后对候选逐条人工审批并写入；默认 dry-run，真写需 ivyea lingxing operate on")
    pp.add_argument("--yes", action="store_true", help="跳过逐条确认（仍受 operate 开关约束）")
    pp.set_defaults(func=_cmd_patrol)

    pd = sub.add_parser("diagnose", help="账户级广告诊断（CSV 汇总：浪费/赢家词/活动/Listing 缺口）")
    pd.add_argument("csv", help="搜索词/广告报表 CSV")
    pd.add_argument("--asin", help="指定 ASIN 画像；留空读取 default 画像")
    pd.add_argument("--target-acos", type=float, dest="target_acos", help="目标 ACoS，如 0.3")
    pd.add_argument("--listing-text", dest="listing_text", help="可选：Listing 标题/五点/A+ 文本，用于检查赢家词缺口")
    pd.add_argument("--min-clicks-no-order", type=int, default=12, help="零单浪费词的最小点击阈值，默认 12")
    pd.add_argument("--top", type=int, default=8, help="每组最多输出条数，默认 8")
    pd.add_argument("--output-dir", dest="output_dir", help="保存 Markdown 报告到目录")
    pd.set_defaults(func=_cmd_diagnose)

    pa = sub.add_parser("apply", help="审核制执行巡检建议（默认 dry-run；--execute 才真写）")
    pa.add_argument("source", help="巡检输出目录 或 *明细*.csv 路径")
    pa.add_argument("--from-mcp", dest="from_mcp", help="执行用的 MCP 服务器（需配 writeActions）")
    pa.add_argument("--execute", action="store_true", help="真实执行（默认仅 dry-run 预览）")
    pa.add_argument("--protected", help="保护词清单，逗号分隔（这些词不否/不动）")
    pa.add_argument("--yes", action="store_true", help="跳过逐条确认，批准所有未被护栏拦截的动作")
    pa.set_defaults(func=_cmd_apply)

    plx = sub.add_parser("lingxing", help="领星 OpenAPI：setup / probe / sellers / operate <on|off|status>")
    plx.add_argument("action", choices=["setup", "probe", "sellers", "operate", "cache"])
    plx.add_argument("value", nargs="?", help="operate 的 on/off/status；cache 的 clear")
    plx.set_defaults(func=_cmd_lingxing)

    pu = sub.add_parser("audit", help="执行审计 / 回滚")
    pu.add_argument("action", choices=["list", "rollback"])
    pu.add_argument("id", nargs="?", help="rollback 的审计ID")
    pu.set_defaults(func=_cmd_audit)

    pob = sub.add_parser("onboard", help="首次运行引导（选模型/配 key/可选领星）")
    pob.set_defaults(func=_cmd_onboard)

    pdoc = sub.add_parser("doctor", help="环境体检：配置、依赖、知识库、磁盘、领星/MCP")
    pdoc.set_defaults(func=_cmd_doctor)

    pself = sub.add_parser("self", help="安装生命周期：status/doctor/backup/upgrade/uninstall")
    pself.add_argument("action", choices=["status", "doctor", "backup", "upgrade", "uninstall"])
    pself.add_argument("--output", help="backup 输出路径")
    pself.add_argument("--version", help="upgrade 固定版本，如 v0.5.5")
    pself.add_argument("--ref", help="upgrade 指定 git ref")
    pself.add_argument("--method", choices=["pipx", "ivyea-runtime", "venv", "unknown"], help="覆盖自动识别的安装方式")
    pself.add_argument("--remove-data", action="store_true", help="uninstall 时同时删除 ~/.ivyea 数据")
    pself.add_argument("--execute", action="store_true", help="真实执行 upgrade/uninstall；默认 dry-run")
    pself.add_argument("--yes", action="store_true", help="执行时跳过交互审批")
    pself.add_argument("--timeout", type=int, default=300)
    pself.set_defaults(func=_cmd_self)

    pact = sub.add_parser("action", help="动作队列：import/list/show/report/approve/deny/done/pending/execute/clear")
    pact.add_argument("action", choices=[
        "import", "list", "show", "report", "approve", "deny", "done", "pending", "execute", "clear"])
    pact.add_argument("source", nargs="?", help="import 的巡检目录/明细CSV；show/approve/execute 的 ID")
    pact.add_argument("--id", help="动作 ID（也可作为第二个位置参数传入）")
    pact.add_argument("--status", help="按状态过滤/清理：pending/approved/denied/done")
    pact.add_argument("--limit", type=int, default=50)
    pact.add_argument("--asin", help="导入时指定 ASIN")
    pact.add_argument("--protected", help="导入时指定保护词，逗号分隔")
    pact.add_argument("--all", action="store_true", help="approve/deny/done/pending 批量更新匹配状态的动作")
    pact.add_argument("--include-blocked", action="store_true", help="批量更新时也包含护栏拦截项")
    pact.add_argument("--summary", action="store_true", help="list 时先输出状态汇总")
    pact.add_argument("--output", help="report 导出 Markdown 到指定路径；留空则打印")
    pact.add_argument("--from-mcp", dest="from_mcp", help="execute 真实写入用的 MCP 服务器（需配 writeActions）")
    pact.add_argument("--execute", action="store_true", help="execute 时真实写入；默认只 dry-run 预演")
    pact.set_defaults(func=_cmd_action)

    pruns = sub.add_parser("runs", help="查看近期巡检和对话会话")
    pruns.add_argument("kind", nargs="?", choices=["all", "patrol", "sessions"], default="all")
    pruns.add_argument("--limit", type=int, default=10)
    pruns.set_defaults(func=_cmd_runs)

    pprof = sub.add_parser("profile", help="运营画像：list / show [default|ASIN|sid:店铺] / set")
    pprof.add_argument("action", choices=["list", "show", "set"])
    pprof.add_argument("key", nargs="?", help="default、ASIN 或 sid:店铺ID")
    pprof.add_argument("--site", help="站点，如 US/UK/DE")
    pprof.add_argument("--target-acos", type=float, dest="target_acos", help="目标 ACOS，如 0.28")
    pprof.add_argument("--margin-rate", type=float, dest="margin_rate", help="毛利率，如 0.35")
    pprof.add_argument("--breakeven-acos", type=float, dest="breakeven_acos", help="盈亏平衡 ACOS，如 0.35")
    pprof.add_argument("--price", type=float, help="当前售价")
    pprof.add_argument("--currency", help="币种，如 USD")
    pprof.add_argument("--stage", help="生命周期阶段，如 launch/growth/mature/clearance")
    pprof.add_argument("--protected", help="保护词，逗号分隔")
    pprof.add_argument("--core", help="核心词，逗号分隔")
    pprof.add_argument("--competitors", help="竞品/对标词，逗号分隔")
    pprof.add_argument("--listing-risks", dest="listing_risks", help="Listing 风险，逗号分隔，如 review弱,主图不清晰")
    pprof.add_argument("--notes", help="打法备注")
    pprof.set_defaults(func=_cmd_profile)

    pscore = sub.add_parser("scorecard", help="运营评估：建议采纳率、执行率、护栏/影子模式概览")
    pscore.add_argument("--limit", type=int, default=1000)
    pscore.add_argument("--output", help="导出 Markdown 到指定路径")
    pscore.set_defaults(func=_cmd_scorecard)

    ptr = sub.add_parser("trace", help="运行时间线：recent / stats")
    ptr.add_argument("action", nargs="?", choices=["recent", "stats"], default="recent")
    ptr.add_argument("--limit", type=int, default=20)
    ptr.add_argument("--session", help="只看指定会话 ID")
    ptr.set_defaults(func=_cmd_trace)

    pev = sub.add_parser("eval", help="业务质量回归：规则引擎/知识召回/skill召回/安全脱敏")
    pev.set_defaults(func=_cmd_eval)

    plis = sub.add_parser("listing", help="Listing 转化诊断：audit")
    plis.add_argument("action", choices=["audit"])
    plis.add_argument("--title")
    plis.add_argument("--title-file")
    plis.add_argument("--bullets")
    plis.add_argument("--bullets-file")
    plis.add_argument("--aplus")
    plis.add_argument("--aplus-file")
    plis.add_argument("--search-terms", help="广告搜索词，逗号分隔")
    plis.add_argument("--reviews", help="Review/Q&A 摘要")
    plis.add_argument("--reviews-file")
    plis.add_argument("--price", type=float)
    plis.add_argument("--rating", type=float)
    plis.add_argument("--review-count", type=int, dest="review_count")
    plis.set_defaults(func=_cmd_listing)

    prev = sub.add_parser("review", help="Review/Q&A/Offer 归因：audit")
    prev.add_argument("action", choices=["audit"])
    prev.add_argument("--reviews", help="Review 摘要/原文片段")
    prev.add_argument("--reviews-file")
    prev.add_argument("--qa", help="Q&A 摘要/原文片段")
    prev.add_argument("--qa-file")
    prev.add_argument("--rating", type=float)
    prev.add_argument("--review-count", type=int, dest="review_count")
    prev.add_argument("--price", type=float)
    prev.add_argument("--coupon")
    prev.add_argument("--competitor-price", type=float, dest="competitor_price")
    prev.set_defaults(func=_cmd_review)

    poff = sub.add_parser("offer", help="Offer/库存/利润诊断：audit")
    poff.add_argument("action", choices=["audit"])
    poff.add_argument("--price", type=float)
    poff.add_argument("--competitor-price", type=float, dest="competitor_price")
    poff.add_argument("--margin-rate", type=float, dest="margin_rate")
    poff.add_argument("--target-acos", type=float, dest="target_acos")
    poff.add_argument("--inventory-days", type=float, dest="inventory_days")
    poff.add_argument("--coupon")
    poff.add_argument("--spend", type=float)
    poff.add_argument("--sales", type=float)
    poff.set_defaults(func=_cmd_offer)

    pcomp = sub.add_parser("competitor", help="竞品/类目关键词诊断：audit")
    pcomp.add_argument("action", choices=["audit"])
    pcomp.add_argument("--own-terms", help="自身核心词，逗号分隔")
    pcomp.add_argument("--search-terms", help="广告搜索词，逗号分隔")
    pcomp.add_argument("--competitor-terms", help="竞品品牌/产品/ASIN，逗号分隔")
    pcomp.add_argument("--category-terms", help="类目/属性词，逗号分隔")
    pcomp.add_argument("--protected-terms", help="保护词，逗号分隔")
    pcomp.set_defaults(func=_cmd_competitor)

    pwk = sub.add_parser("weekly", help="周期运营复盘：review")
    pwk.add_argument("action", choices=["review"])
    pwk.add_argument("--limit", type=int, default=200)
    pwk.add_argument("--output", help="导出 Markdown 到指定路径")
    pwk.set_defaults(func=_cmd_weekly)

    palert = sub.add_parser("alert", help="本地自动预警：check")
    palert.add_argument("action", choices=["check"])
    palert.add_argument("--limit", type=int, default=500)
    palert.add_argument("--notify", action="store_true", help="将预警发送到通知通道")
    palert.add_argument("--channel", choices=["stdout", "webhook", "feishu"], default="stdout")
    palert.add_argument("--webhook-url", help="覆盖 settings/env 中的 webhook URL")
    palert.add_argument("--title", help="通知标题")
    palert.set_defaults(func=_cmd_alert)

    pnot = sub.add_parser("notify", help="通知通道：test")
    pnot.add_argument("action", choices=["test"])
    pnot.add_argument("--message", help="测试消息")
    pnot.add_argument("--title", help="通知标题")
    pnot.add_argument("--channel", choices=["stdout", "webhook", "feishu"], default="stdout")
    pnot.add_argument("--webhook-url", help="覆盖 settings/env 中的 webhook URL")
    pnot.set_defaults(func=_cmd_notify)

    psch = sub.add_parser("schedule", help="本地计划任务：list/set/remove/run-due/run")
    psch.add_argument("action", choices=["list", "set", "remove", "run-due", "run"])
    psch.add_argument("name", nargs="?", help="set/remove 的计划名称")
    psch.add_argument("task", nargs="?", help="set/run 的任务：alert/weekly/eval")
    psch.add_argument("--every-hours", type=float, default=24.0)
    psch.add_argument("--limit", type=int, default=500)
    psch.add_argument("--notify", action="store_true", help="alert 任务完成后发送通知")
    psch.add_argument("--channel", choices=["stdout", "webhook", "feishu"], default="stdout")
    psch.add_argument("--webhook-url", help="覆盖 settings/env 中的 webhook URL")
    psch.add_argument("--title", help="通知标题")
    psch.set_defaults(func=_cmd_schedule)

    ppol = sub.add_parser("policy", help="本地安全策略：show/init/check-path/check-command/explain-command")
    ppol.add_argument("action", choices=["show", "init", "check-path", "check-command", "explain-command"])
    ppol.add_argument("value", nargs="?")
    ppol.add_argument("--op", choices=["read", "write"], default="read")
    ppol.add_argument("--force", action="store_true")
    ppol.set_defaults(func=_cmd_policy)

    pws = sub.add_parser("workspace", help="通用项目理解：index/search/map/graph/inspect/symbols/impact/explain")
    pws.add_argument("action", choices=["index", "search", "map", "graph", "inspect", "symbols", "impact", "explain"])
    pws.add_argument("query", nargs="?", help="search 查询词；explain 目标路径")
    pws.add_argument("--root", default=".", help="项目根目录，默认当前目录")
    pws.add_argument("--target", help="explain 的目标文件/目录；不填则使用 query 或 .")
    pws.add_argument("--limit", type=int, default=10)
    pws.add_argument("--max-files", type=int, default=2000)
    pws.add_argument("--max-bytes", type=int, default=256_000)
    pws.add_argument("--include-hidden", action="store_true", help="包含隐藏目录/文件")
    pws.add_argument("--refresh", action="store_true", help="map/explain 前强制重建索引")
    pws.set_defaults(func=_cmd_workspace)

    ptask = sub.add_parser("task", help="通用长任务：create/list/show/start/step/status/log/resume")
    ptask.add_argument("action", choices=["create", "list", "show", "start", "step", "status", "log", "resume"])
    ptask.add_argument("id", nargs="?", help="任务 ID（show/start/step/status/log/resume）")
    ptask.add_argument("--title", help="create 的任务标题")
    ptask.add_argument("--step", action="append", help="create 的步骤，可重复")
    ptask.add_argument("--steps", help="create 的步骤，用 | 分隔")
    ptask.add_argument("--index", type=int, default=1, help="step 的步骤序号")
    ptask.add_argument("--status", help="list 过滤任务状态；step/status 设置状态")
    ptask.add_argument("--notes", help="备注/日志")
    ptask.add_argument("--workspace", help="关联工作区路径")
    ptask.add_argument("--limit", type=int, default=20)
    ptask.set_defaults(func=_cmd_task)

    pgit = sub.add_parser("gitops", help="Git/CI 工作流：status/diff/workflows/ci/release-plan/stage/commit/tag")
    pgit.add_argument("action", choices=["status", "diff", "workflows", "ci", "release-plan", "stage", "commit", "tag"])
    pgit.add_argument("--root", default=".")
    pgit.add_argument("--remote", default="origin", help="ci 使用的 GitHub remote，默认 origin")
    pgit.add_argument("--staged", action="store_true", help="diff 查看 staged 变更")
    pgit.add_argument("--version", help="release-plan 检查的版本，如 v0.5.5")
    pgit.add_argument("--file", action="append", help="stage 指定文件；可重复。不传则 stage -A")
    pgit.add_argument("--message", help="commit message")
    pgit.add_argument("--tag", help="tag 名称，如 v0.5.5")
    pgit.add_argument("--execute", action="store_true", help="真实执行；默认只预览")
    pgit.add_argument("--yes", action="store_true", help="写操作跳过交互审批")
    pgit.add_argument("--limit", type=int, default=5, help="ci 显示最近 run 数量")
    pgit.add_argument("--timeout", type=int, default=30)
    pgit.set_defaults(func=_cmd_gitops)

    pcoderev = sub.add_parser("codereview", help="代码审查：只读扫描 git diff 风险")
    pcoderev.add_argument("--root", default=".")
    pcoderev.add_argument("--staged", action="store_true", help="审查 staged diff")
    pcoderev.set_defaults(func=_cmd_codereview)

    pcode = sub.add_parser("code", help="代码 Agent 闭环：plan/context/brief/quality/refs/rename-plan/diff-brief/release-check/run/runs/show/sandbox/impact/patch/test/repair/review")
    pcode.add_argument("action", choices=["plan", "context", "brief", "quality", "refs", "rename-plan", "diff-brief", "release-check", "run", "runs", "show", "sandbox", "impact", "patch", "test", "repair", "review"])
    pcode.add_argument("goal", nargs="?", help="plan/context/run 的自然语言目标；refs/rename-plan 的符号；show 的 run id")
    pcode.add_argument("--target", help="impact 的符号、模块或文件目标")
    pcode.add_argument("--path", help="patch 候选目标文件")
    pcode.add_argument("--old", help="patch 候选原文；必须在目标文件中唯一匹配")
    pcode.add_argument("--new", help="patch 候选新文本")
    pcode.add_argument("--llm", action="store_true", help="patch 时生成 LLM 请求包；默认不调用模型")
    pcode.add_argument("--llm-patch", action="store_true", help="code run 时在 patch 阶段生成 LLM patch 请求包")
    pcode.add_argument("--call", action="store_true", help="与 --llm 配合，真实调用当前模型生成 patch 并 dry-run validate")
    pcode.add_argument("--root", default=".")
    pcode.add_argument("--limit", type=int, default=8, help="context 输出文件数量")
    pcode.add_argument("--budget", type=int, default=6000, help="brief 字符预算")
    pcode.add_argument("--command", dest="test_command", help="test 要运行的命令，默认 python -m pytest")
    pcode.add_argument("--run-tests", action="store_true", help="code run 时真实运行测试；默认只列建议测试")
    pcode.add_argument("--max-rounds", type=int, default=1, help="code run 最大轮次；当前骨架只执行首轮 dry-run")
    pcode.add_argument("--name", help="sandbox 计划名称")
    pcode.add_argument("--timeout", type=int, default=120)
    pcode.add_argument("--output-file", help="repair 读取 pytest 输出文件")
    pcode.add_argument("--text", help="repair 直接传入失败输出文本；不传则读取 stdin")
    pcode.add_argument("--staged", action="store_true", help="review 审查 staged diff")
    pcode.add_argument("--version", help="release-check 版本，如 v0.5.6；不传则读取 pyproject.toml")
    pcode.add_argument("--new-name", help="rename-plan 的新符号名")
    pcode.set_defaults(func=_cmd_code)

    ppatch = sub.add_parser("patch", help="结构化补丁：make/validate/apply/tests/run-tests")
    ppatch.add_argument("action", choices=["make", "validate", "apply", "tests", "run-tests"])
    ppatch.add_argument("spec", nargs="?", help="validate/apply 的 patch JSON")
    ppatch.add_argument("--root", default=".")
    ppatch.add_argument("--path", help="make 的目标文件")
    ppatch.add_argument("--old", help="make 的原文")
    ppatch.add_argument("--new", help="make 的新文")
    ppatch.add_argument("--output", help="make 输出 JSON 文件")
    ppatch.add_argument("--execute", action="store_true", help="apply 时真实写入；默认 dry-run")
    ppatch.add_argument("--yes", action="store_true", help="apply --execute 时跳过交互审批")
    ppatch.add_argument("--command", dest="test_command", help="run-tests 的测试命令")
    ppatch.add_argument("--timeout", type=int, default=120)
    ppatch.set_defaults(func=_cmd_patch)

    pvis = sub.add_parser("vision", help="通用多模态视觉：截图/UI/报表 inspect")
    pvis.add_argument("paths", nargs="+", help="图片文件或目录")
    pvis.add_argument("--task", help="希望模型完成的视觉任务")
    pvis.add_argument("--context", help="业务/页面/报表上下文")
    pvis.add_argument("--provider", choices=["openai", "anthropic", "gemini"], default="openai")
    pvis.add_argument("--model", help="覆盖默认视觉模型名")
    pvis.add_argument("--max-images", type=int, default=8)
    pvis.add_argument("--payload", action="store_true", help="显示截断后的 payload 预览")
    pvis.add_argument("--output", help="导出截断后的请求包 JSON")
    pvis.add_argument("--call", action="store_true", help="真实调用多模态模型；默认只生成请求包")
    pvis.add_argument("--api-key", help="覆盖 provider 对应环境变量中的 API key")
    pvis.add_argument("--timeout", type=float, default=120.0)
    pvis.set_defaults(func=_cmd_vision)

    pimg = sub.add_parser("image", help="Listing 图片资产诊断：audit / ocr / vision")
    pimg.add_argument("action", choices=["audit", "ocr", "vision"])
    pimg.add_argument("paths", nargs="+", help="图片文件或目录")
    pimg.add_argument("--no-recursive", action="store_true", help="目录扫描不递归")
    pimg.add_argument("--prompt", action="store_true", help="输出多模态大模型审核 prompt")
    pimg.add_argument("--prompt-out", help="把多模态 prompt 写到文件")
    pimg.add_argument("--context", help="产品/广告上下文，写入 prompt")
    pimg.add_argument("--provider", choices=["openai", "anthropic", "gemini"], default="openai")
    pimg.add_argument("--model", help="覆盖默认视觉模型名")
    pimg.add_argument("--lang", default="eng", help="OCR 语言，如 eng/chi_sim/eng+chi_sim")
    pimg.add_argument("--max-images", type=int, default=8)
    pimg.add_argument("--payload", action="store_true", help="显示截断后的 payload 预览")
    pimg.add_argument("--output", help="导出截断后的请求包 JSON")
    pimg.add_argument("--call", action="store_true", help="真实调用多模态模型；默认只生成请求包")
    pimg.add_argument("--api-key", help="覆盖 provider 对应环境变量中的 API key")
    pimg.add_argument("--timeout", type=float, default=120.0, help="真实调用超时时间（秒）")
    pimg.set_defaults(func=_cmd_image)

    psh = sub.add_parser("shadow", help="影子模式：on/off（只记不写）/ list / report（回测若照做的收益）")
    psh.add_argument("action", choices=["on", "off", "list", "report"])
    psh.add_argument("--sid", help="店铺 SID（report/list）")
    psh.add_argument("--days", type=int, default=14, help="回测窗口天数，默认 14")
    psh.set_defaults(func=_cmd_shadow)

    pmo = sub.add_parser("model", help="查看/配置主脑模型（交互；或 ivyea model deepseek:deepseek-chat）")
    pmo.add_argument("spec", nargs="?", help="provider:model，如 deepseek:deepseek-chat")
    pmo.add_argument("extra", nargs="?", help="auth/logout 时的 provider id")
    pmo.add_argument("--token", help="为 OAuth/Bearer provider 保存 access token")
    pmo.add_argument("--refresh-token", help="为 OAuth provider 保存 refresh token（可选）")
    pmo.add_argument("--expires-at", type=float, default=0, help="access token 过期时间戳（可选）")
    pmo.add_argument("--project", help="为 google-gemini-cli 保存 Google Cloud project id")
    pmo.add_argument("--probe", action="store_true", help="真实探测 provider 是否可用（支持 Gemini/Codex/Copilot/Qwen）")
    pmo.add_argument("--timeout", type=float, default=30.0, help="auth probe 超时时间（秒）")
    pmo.add_argument("--refresh", action="store_true", help="刷新 OAuth access token（支持 qwen-oauth/openai-codex/google-gemini-cli）")
    pmo.add_argument("--login", action="store_true", help="运行浏览器/外部 OAuth 登录（支持 google-gemini-cli/qwen-oauth）")
    pmo.add_argument("--no-browser", action="store_true", help="OAuth 登录时不自动打开浏览器，改为手动粘贴 callback URL/code")
    pmo.add_argument("--device-code", action="store_true", help="运行 OAuth device-code 登录（支持 openai-codex/qwen-oauth）")
    pmo.add_argument("--exchange", action="store_true", help="验证并换取短期 API token（目前支持 copilot）")
    pmo.add_argument("--import-qwen-cli", action="store_true", help="从 ~/.qwen/oauth_creds.json 导入 qwen-oauth 凭证")
    pmo.set_defaults(func=_cmd_model)

    pmem = sub.add_parser("memory", help="记忆：status（默认）/ search <词> / note [asin]")
    pmem.add_argument("action", nargs="?", choices=["status", "search", "note"], default="status")
    pmem.add_argument("query", nargs="?")
    pmem.set_defaults(func=_cmd_memory)

    pk = sub.add_parser("knowledge", help="亚马逊知识库：list/search/show/audit/import/url/rebuild/index/conflicts")
    pk.add_argument("action", choices=["list", "search", "show", "audit", "import", "url", "rebuild", "index", "conflicts"])
    pk.add_argument("query", nargs="?")
    pk.add_argument("--limit", type=int, default=5)
    pk.add_argument("--id", help="导入时指定知识 ID，如 user.my-playbook")
    pk.add_argument("--title", help="导入时指定标题")
    pk.add_argument("--source-type", dest="source_type", help="来源类型：official/community/user 等")
    pk.add_argument("--confidence", help="可信度：high/medium/user_supplied 等")
    pk.add_argument("--license", help="来源许可/授权说明，如 user_supplied/public_summary")
    pk.add_argument("--tags", help="标签，逗号分隔")
    pk.set_defaults(func=_cmd_knowledge)

    pski = sub.add_parser("skill", help="可复用 Skill：list/search/show/run/create/audit/status/export-lock")
    pski.add_argument("action", choices=["list", "search", "show", "run", "create", "audit", "status", "export-lock"])
    pski.add_argument("query", nargs="?")
    pski.add_argument("--limit", type=int, default=8)
    pski.add_argument("--title")
    pski.add_argument("--domain")
    pski.add_argument("--description")
    pski.add_argument("--trigger", action="append")
    pski.add_argument("--tool", action="append")
    pski.add_argument("--knowledge", action="append")
    pski.add_argument("--body")
    pski.add_argument("--body-file")
    pski.add_argument("--output")
    pski.add_argument("--force", action="store_true")
    pski.set_defaults(func=_cmd_skill)

    pch = sub.add_parser("chat", help="对话式 Agent（自然语言 + 斜杠命令 + 人工审批）")
    pch.add_argument("--from-mcp", dest="from_mcp", help="执行/拉数用的 MCP 服务器")
    pch.add_argument("--execute", action="store_true", help="允许真实写（默认 dry-run）")
    pch.add_argument("--asin", help="本轮对话使用的 ASIN 画像")
    pch.add_argument("--protected", help="保护词清单，逗号分隔")
    pch.add_argument("--task-id", help="绑定已有 `ivyea task` 长任务；工具上限/中断会自动写入任务日志")
    pch.add_argument("--resume", nargs="?", const=True, help="续接会话：留空=最近一个，或指定会话ID")
    pch.add_argument("--continue", dest="cont", action="store_true", help="续接最近一个会话")
    pch.add_argument("--raw", action="store_true", help="原始流式输出（默认 Markdown 渲染）")
    pch.set_defaults(func=_cmd_chat)
    return p


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # 像 claude/hermes：直接敲 `ivyea` 进对话模式（dry-run 默认）
        chat_argv = ["chat"]
        if getattr(args, "cont", False):
            chat_argv.append("--continue")
        if getattr(args, "raw", False):
            chat_argv.append("--raw")
        r = getattr(args, "resume", None)
        if r is True:
            chat_argv.append("--resume")
        elif r:
            chat_argv += ["--resume", r]
        args = parser.parse_args(chat_argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
