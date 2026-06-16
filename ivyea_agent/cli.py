"""Ivyea Agent CLI 入口。

P1 子命令：
  ivyea config show
  ivyea config set <key> <value>
  ivyea patrol <搜索词报告.csv> [--asin B0..] [--site US] [--target-acos 0.3] [--no-llm]
"""
from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys

from . import __version__, config


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


def _config_wizard() -> int:
    """交互式配置向导（一条 `ivyea config` 即进）。"""
    config.ensure_dirs()
    s = config.load_settings()
    print("── Ivyea Agent 配置向导（回车=保留当前值）──\n")
    provider = _ask("主脑模型 provider (deepseek/openai/anthropic)", s.get("provider", "deepseek"))
    model = _ask("模型名", s.get("model", "deepseek-chat"))
    site = _ask("默认站点", s.get("site", "US"))
    acos_raw = _ask("目标 ACoS (如 0.3)", str(s.get("target_acos", 0.3)))
    try:
        target_acos = float(acos_raw)
    except ValueError:
        print(f"  (target_acos '{acos_raw}' 非数字，保留 {s.get('target_acos')})")
        target_acos = s.get("target_acos", 0.3)
    config.save_settings({**s, "provider": provider, "model": model, "site": site,
                          "target_acos": target_acos})

    has_key = bool(config.get_api_key(provider))
    status = "已配置" if has_key else "未配置"
    print(f"\n{provider} API key 当前：{status}")
    newkey = _ask_secret(f"输入 {provider} API key（回车跳过；输入 - 清空）")
    if newkey == "-":
        config.set_api_key(provider, "")
        print("  已清空。")
    elif newkey:
        config.set_api_key(provider, newkey)
        print("  已保存到 ~/.ivyea/.env")

    print("\n✓ 配置已保存。再看一眼：\n")
    return _print_config()


def _print_config() -> int:
    s = config.load_settings()
    print(f"配置目录: {config.IVYEA_DIR}")
    print(f".env:      {config.ENV_FILE} ({'存在' if config.ENV_FILE.exists() else '缺失'})")
    print(f"mcp.json:  {config.MCP_FILE} ({'存在' if config.MCP_FILE.exists() else '缺失'})")
    print("settings:")
    for k, v in s.items():
        print(f"  {k} = {v}")
    provider = s.get("provider", "deepseek")
    print(f"主脑 key ({provider}): {'已配置' if config.get_api_key(provider) else '未配置'}")
    servers = config.load_mcp().get("mcpServers", {})
    print(f"MCP 服务器: {', '.join(servers) if servers else '(无，用 ivyea mcp add 添加)'}")
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
    if args.action == "remove":
        if not args.name:
            print("用法: ivyea mcp remove <名称>", file=sys.stderr)
            return 2
        ok = config.mcp_remove_server(args.name)
        print("已删除。" if ok else f"未找到服务器 '{args.name}'。")
        return 0 if ok else 1
    return 2


def _cmd_patrol(args: argparse.Namespace) -> int:
    from . import patrol as patrol_mod
    from .rule_engine import RuleEngineError
    try:
        result = patrol_mod.patrol(
            args.csv, asin=args.asin, site=args.site, target_acos=args.target_acos,
            report_type=args.report_type, output_dir=args.output_dir, use_llm=not args.no_llm)
    except RuleEngineError as e:
        print(f"[规则引擎错误] {e}", file=sys.stderr)
        return 1
    print(result["text"])
    print(f"\n[已保存] {result['md_path']}", file=sys.stderr)
    if not result["review"]["ok"] and not args.no_llm:
        print(f"[提示] {result['review']['note']}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ivyea", description="Ivyea Agent — 亚马逊运营 CLI Agent")
    p.add_argument("--version", action="version", version=f"ivyea-agent {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("config", help="配置向导（无参=交互式）/ show / set / edit")
    pc.add_argument("action", nargs="?", choices=["show", "set", "edit"], default=None)
    pc.add_argument("key", nargs="?")
    pc.add_argument("value", nargs="?")
    pc.set_defaults(func=_cmd_config)

    pm = sub.add_parser("mcp", help="MCP 服务器配置（对话式 add / list / remove）")
    pm.add_argument("action", choices=["add", "list", "remove"])
    pm.add_argument("name", nargs="?")
    pm.set_defaults(func=_cmd_mcp)

    pp = sub.add_parser("patrol", help="只读广告巡检（输入搜索词报告 CSV）")
    pp.add_argument("csv", help="搜索词报告路径 (csv/xlsx)")
    pp.add_argument("--asin", help="指定分析的 ASIN")
    pp.add_argument("--site", help="站点代码，默认取配置/US")
    pp.add_argument("--target-acos", type=float, dest="target_acos", help="目标 ACoS，如 0.3")
    pp.add_argument("--report-type", dest="report_type", help="SP/SB/SD")
    pp.add_argument("--output-dir", dest="output_dir", help="输出目录")
    pp.add_argument("--no-llm", action="store_true", help="只跑规则引擎，跳过 AI 复核")
    pp.set_defaults(func=_cmd_patrol)
    return p


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
