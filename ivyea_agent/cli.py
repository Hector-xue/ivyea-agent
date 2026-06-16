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
    if args.action in ("tools", "call"):
        from .mcp_client import MCPClient, MCPError
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
            # call
            if not args.tool:
                print("用法: ivyea mcp call <名称> <工具> [--args '{\"k\":\"v\"}']", file=sys.stderr)
                return 2
            arguments = {}
            if args.args:
                try:
                    arguments = __import__("json").loads(args.args)
                except Exception as e:
                    print(f"--args 不是合法 JSON: {e}", file=sys.stderr)
                    return 2
            res = client.call_tool(args.tool, arguments)
            print(__import__("json").dumps(res, ensure_ascii=False, indent=2)[:4000])
            return 0
        except MCPError as e:
            print(f"[MCP 错误] {e}", file=sys.stderr)
            return 1
    return 2


def _cmd_patrol(args: argparse.Namespace) -> int:
    from . import patrol as patrol_mod
    from .rule_engine import RuleEngineError

    csv_path = args.csv
    if args.from_mcp:
        from .mcp_source import fetch_to_csv
        from .mcp_client import MCPError
        if not args.asin:
            print("--from-mcp 需要 --asin（按 ASIN 拉广告搜索词数据）", file=sys.stderr)
            return 2
        site = args.site or config.get_setting("site", "US")
        try:
            print(f"[MCP] 从 '{args.from_mcp}' 拉取 {args.asin} 近 {args.days} 天广告数据…", file=sys.stderr)
            csv_path = fetch_to_csv(args.from_mcp, args.asin, site, days=args.days)
            print(f"[MCP] 已拉取并转换为: {csv_path}", file=sys.stderr)
        except MCPError as e:
            print(f"[MCP 错误] {e}", file=sys.stderr)
            return 1
    if not csv_path:
        print("需要提供搜索词报告 CSV，或用 --from-mcp <服务器> 自动拉数。", file=sys.stderr)
        return 2
    try:
        args.csv = csv_path
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


def _cmd_apply(args: argparse.Namespace) -> int:
    from . import actions as act_mod, executor, guardrails
    from pathlib import Path

    detail = args.source
    if Path(args.source).is_dir():
        detail = act_mod.load_detail_from_dir(args.source)
    if not detail or not Path(detail).exists():
        print(f"找不到巡检明细 CSV（传入巡检输出目录或 *明细*.csv）：{args.source}", file=sys.stderr)
        return 2

    protected = [w for w in (args.protected or "").split(",") if w.strip()]
    acts = guardrails.annotate(act_mod.extract_actions(detail), protected_terms=protected)
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
        if decision == permission.APPROVE:
            confirmed.append(a)

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
            print(f"  {e.get('id','?')}  {e.get('ts','')}  {e.get('kind','')}  "
                  f"{e.get('search_term','')}  [{e.get('server','')}]")
        return 0
    if args.action == "rollback":
        if not args.id:
            print("用法: ivyea audit rollback <审计ID>", file=sys.stderr)
            return 2
        r = executor.rollback(args.id)
        print(("✓ " if r["ok"] else "✗ ") + r["detail"])
        return 0 if r["ok"] else 1
    return 2


_CHAT_HELP = """斜杠命令：
  /help              显示帮助
  /model [p:model]   查看/切换主脑模型（如 /model deepseek:deepseek-chat）
  /mcp               列出已配置的 MCP 服务器
  /tools             列出 Agent 可用工具
  /clear             清空当前对话上下文
  /memory            记忆（P3 规划中）
  /exit | /quit      退出
直接输入自然语言即可（如：看下 B0XXXXXXXX 这周广告，数据用 sample CSV）。
写操作会逐条弹出人工审批，未确认不会执行。"""


def _cmd_chat(args: argparse.Namespace) -> int:
    from . import agent_loop, agent_tools, config as cfg
    from .providers import get_provider, LLMError

    s = cfg.load_settings()
    provider_name, model = s.get("provider", "deepseek"), s.get("model", "deepseek-chat")
    api_key = cfg.get_api_key(provider_name)
    ctx = agent_tools.ToolContext(
        from_mcp=args.from_mcp, execute=args.execute,
        protected=[w for w in (args.protected or "").split(",") if w.strip()])
    messages = [{"role": "system", "content": agent_loop.SYSTEM_PROMPT}]

    print("Ivyea Agent · 对话模式（输入 /help 看命令，/exit 退出）")
    print(f"主脑: {provider_name}:{model} ({'已配置' if api_key else '未配置 key — 自然语言对话不可用，斜杠命令可用'}) "
          f"| 执行: {'真实写(--execute)' if args.execute else 'dry-run'}"
          f"{' via '+args.from_mcp if args.from_mcp else ''}\n")

    while True:
        try:
            line = input("你 › ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit"):
            print("再见。")
            return 0
        if line == "/help":
            print(_CHAT_HELP); continue
        if line == "/clear":
            messages = [{"role": "system", "content": agent_loop.SYSTEM_PROMPT}]
            print("（已清空对话上下文）"); continue
        if line == "/mcp":
            servers = cfg.load_mcp().get("mcpServers", {})
            print("MCP 服务器: " + (", ".join(servers) if servers else "(无，ivyea mcp add)")); continue
        if line == "/tools":
            for t in agent_tools.TOOL_SCHEMAS:
                f = t["function"]; print(f"  {f['name']} — {f['description']}")
            continue
        if line == "/memory":
            print("记忆系统（SQLite FTS5 + 策展 markdown）在 P3 实现，敬请期待。"); continue
        if line.startswith("/model"):
            parts = line.split()
            if len(parts) == 1:
                print(f"当前主脑: {provider_name}:{model}")
            else:
                pm = parts[1]
                provider_name, _, m = pm.partition(":")
                model = m or model
                cfg.set_setting("provider", provider_name); cfg.set_setting("model", model)
                api_key = cfg.get_api_key(provider_name)
                print(f"已切换主脑: {provider_name}:{model} ({'已配置' if api_key else '未配置 key'})")
            continue
        if line.startswith("/"):
            print(f"未知命令 {line}，/help 看帮助"); continue

        # 自然语言 → Agent 循环
        if not api_key:
            print(f"⚠️ 未配置 {provider_name} 的 API key，自然语言对话不可用。"
                  f"先 `ivyea config` 配置，或用斜杠命令。")
            continue
        messages.append({"role": "user", "content": line})
        try:
            provider = get_provider(provider_name, api_key, model)
            reply = agent_loop.run_turn(provider, ctx, messages)
            print(f"\nIvyea › {reply}\n")
        except LLMError as e:
            print(f"[模型错误] {e}")
            messages.pop()  # 撤回这条 user，避免污染上下文


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ivyea", description="Ivyea Agent — 亚马逊运营 CLI Agent")
    p.add_argument("--version", action="version", version=f"ivyea-agent {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("config", help="配置向导（无参=交互式）/ show / set / edit")
    pc.add_argument("action", nargs="?", choices=["show", "set", "edit"], default=None)
    pc.add_argument("key", nargs="?")
    pc.add_argument("value", nargs="?")
    pc.set_defaults(func=_cmd_config)

    pm = sub.add_parser("mcp", help="MCP 配置/自检（add/list/remove/edit/tools/call）")
    pm.add_argument("action", choices=["add", "list", "remove", "edit", "tools", "call"])
    pm.add_argument("name", nargs="?", help="服务器名（remove/tools/call 需要）")
    pm.add_argument("tool", nargs="?", help="工具名（call 需要）")
    pm.add_argument("--args", help="call 的入参 JSON，如 '{\"asin\":\"B0..\"}'")
    pm.set_defaults(func=_cmd_mcp)

    pp = sub.add_parser("patrol", help="只读广告巡检（输入 CSV 或 --from-mcp 自动拉数）")
    pp.add_argument("csv", nargs="?", help="搜索词报告路径 (csv/xlsx)；用 --from-mcp 时可省略")
    pp.add_argument("--from-mcp", dest="from_mcp", help="改用已配置的 MCP 服务器拉广告数据（需该服务器配好 dataSource 映射）")
    pp.add_argument("--days", type=int, default=30, help="MCP 拉取天数，默认 30")
    pp.add_argument("--asin", help="指定分析的 ASIN（--from-mcp 时必填）")
    pp.add_argument("--site", help="站点代码，默认取配置/US")
    pp.add_argument("--target-acos", type=float, dest="target_acos", help="目标 ACoS，如 0.3")
    pp.add_argument("--report-type", dest="report_type", help="SP/SB/SD")
    pp.add_argument("--output-dir", dest="output_dir", help="输出目录")
    pp.add_argument("--no-llm", action="store_true", help="只跑规则引擎，跳过 AI 复核")
    pp.set_defaults(func=_cmd_patrol)

    pa = sub.add_parser("apply", help="审核制执行巡检建议（默认 dry-run；--execute 才真写）")
    pa.add_argument("source", help="巡检输出目录 或 *明细*.csv 路径")
    pa.add_argument("--from-mcp", dest="from_mcp", help="执行用的 MCP 服务器（需配 writeActions）")
    pa.add_argument("--execute", action="store_true", help="真实执行（默认仅 dry-run 预览）")
    pa.add_argument("--protected", help="保护词清单，逗号分隔（这些词不否/不动）")
    pa.add_argument("--yes", action="store_true", help="跳过逐条确认，批准所有未被护栏拦截的动作")
    pa.set_defaults(func=_cmd_apply)

    pu = sub.add_parser("audit", help="执行审计 / 回滚")
    pu.add_argument("action", choices=["list", "rollback"])
    pu.add_argument("id", nargs="?", help="rollback 的审计ID")
    pu.set_defaults(func=_cmd_audit)

    pch = sub.add_parser("chat", help="对话式 Agent（自然语言 + 斜杠命令 + 人工审批）")
    pch.add_argument("--from-mcp", dest="from_mcp", help="执行/拉数用的 MCP 服务器")
    pch.add_argument("--execute", action="store_true", help="允许真实写（默认 dry-run）")
    pch.add_argument("--protected", help="保护词清单，逗号分隔")
    pch.set_defaults(func=_cmd_chat)
    return p


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
