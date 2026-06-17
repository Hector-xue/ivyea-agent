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


def _model_picker() -> None:
    """像 Hermes/Claude：列出国内外主流模型 + 登录制，选编号配置。"""
    from . import models
    config.ensure_dirs()
    s = config.load_settings()
    print(f"\n当前主脑: {_C['c']}{s.get('label', s.get('provider'))}{_C['x']} "
          f"（{s.get('model')}，{'已配 key' if config.get_active_key() else '未配 key'}）\n")
    idx, n = {}, 1
    for group, items in models.grouped():
        print(f"{_C['b']}{group}{_C['x']}")
        for m in items:
            tag = {"openai": "",
                   "native": f"{_C['d']} (原生API·规划中){_C['x']}",
                   "login": f"{_C['d']} (登录制·规划中){_C['x']}"}.get(m["kind"], "")
            print(f"  {_C['c']}{n:>2}{_C['x']}) {m['label']}{tag}")
            idx[str(n)] = m; n += 1
    choice = _ask("\n选择编号（回车取消）")
    m = idx.get(choice)
    if not m:
        print("已取消。"); return
    model, base = m.get("model", ""), m.get("base", "")
    if m["id"] == "custom":
        base = _ask("base_url（OpenAI 兼容，如 https://xxx/v1）", base)
        model = _ask("model 名", model)
    elif m["kind"] == "openai":
        model = _ask("model 名（回车用默认）", model)
    config.apply_model(m, model=model, base_url=base)
    if m["kind"] in ("openai", "anthropic"):
        cur = "已配置" if config.get_active_key() else "未配置"
        nk = _ask_secret(f"{m['label']} 的 API key（{m['key_env']}，当前{cur}；回车跳过 / - 清空）")
        if nk == "-":
            config.set_env_key(m["key_env"], "")
        elif nk:
            config.set_env_key(m["key_env"], nk)
        print(f"✓ 已切换主脑：{m['label']}（{model or m.get('model')}），"
              f"{'已配 key' if config.get_active_key() else '未配 key'}")
    else:
        print(f"已选 {m['label']}，但{m.get('note', '该类型规划中')}。"
              f"当前可直接用：Claude 原生 + OpenAI 兼容类（DeepSeek/通义/Kimi/GLM/豆包/MiniMax/OpenRouter/OpenAI/自定义）。")


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
    print(f"配置目录: {config.IVYEA_DIR}")
    print(f".env:      {config.ENV_FILE} ({'存在' if config.ENV_FILE.exists() else '缺失'})")
    print(f"mcp.json:  {config.MCP_FILE} ({'存在' if config.MCP_FILE.exists() else '缺失'})")
    print(f"主脑模型: {s.get('label', s.get('provider'))} · {s.get('model')} · kind={s.get('kind')}"
          f" · key {'已配置' if config.get_active_key() else '未配置'}")
    print(f"站点: {s.get('site')}　目标 ACoS: {s.get('target_acos')}")
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

    if getattr(args, "from_lingxing", False):
        return _patrol_lingxing(args)

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
    try:
        print(f"[领星] 店铺 sid={args.sid} 拉取近 {args.days} 天广告报表并跑规则引擎（逐日聚合，可能耗时）…",
              file=sys.stderr)
        result = opt.run_store(int(args.sid), days=args.days)
    except LingXingError as e:
        print(f"[领星错误] {e}", file=sys.stderr)
        return 1
    print(lrep.render(result, color=sys.stdout.isatty()))
    out_dir = args.output_dir or str(config.IVYEA_DIR / "patrol_out")
    md_path = report.write_md(lrep.render_md(result), out_dir, asin=f"sid{args.sid}")
    print(f"\n[已保存] {md_path}", file=sys.stderr)

    if getattr(args, "execute", False):
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


def _cmd_apply(args: argparse.Namespace) -> int:
    from . import actions as act_mod, executor, guardrails, memory
    from pathlib import Path

    detail = args.source
    asin = ""
    if Path(args.source).is_dir():
        detail = act_mod.load_detail_from_dir(args.source)
        asin = act_mod.asin_from_dir(args.source)
    if not detail or not Path(detail).exists():
        print(f"找不到巡检明细 CSV（传入巡检输出目录或 *明细*.csv）：{args.source}", file=sys.stderr)
        return 2

    protected = [w for w in (args.protected or "").split(",") if w.strip()]
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
    ("/memory", "记忆：状态/最近巡检；/memory <词> 检索"),
    ("/plan", "进入/退出计划模式（只读，不写入）"),
    ("/approve", "批准并退出计划模式，继续执行"),
    ("/cost", "本会话 token 用量与成本估算"),
    ("/compact", "压缩上下文（LLM 摘要历史，省 token）"),
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
    from . import agent_loop, agent_tools, config as cfg, pricing, sessions, context as ctx_mod, markdown, memory
    from .providers import from_settings, LLMError

    def _label() -> str:
        s = cfg.load_settings()
        return s.get("label", s.get("provider", "deepseek"))

    ctx = agent_tools.ToolContext(
        from_mcp=args.from_mcp, execute=args.execute, workspace=os.getcwd(),
        protected=[w for w in (args.protected or "").split(",") if w.strip()])
    meter = pricing.UsageMeter()
    _ui = {"ctx": 0}                                        # 状态栏:上下文 token 估算
    instructions = memory.load_instructions(os.getcwd())   # USER.md/AGENTS.md 持久指令

    def _sys_msg() -> dict:
        content = agent_loop.SYSTEM_PROMPT + (agent_loop.PLAN_NOTE if ctx.plan_mode else "")
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
    render_md = not getattr(args, "raw", False)   # 默认 markdown 渲染

    def _persist():
        try:
            sessions.save(sid, messages, model=cfg.get_model_config().get("model", ""),
                          usage={"cost": meter.cost, "turns": meter.turns,
                                 "prompt": meter.prompt, "completion": meter.completion})
        except Exception:
            pass

    keyst = "已配置" if cfg.get_active_key() else "未配 key（/model 配置后可对话）"
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
            print("（已清空对话上下文）"); continue
        if line == "/plan":
            ctx.plan_mode = not ctx.plan_mode
            messages[0] = _sys_msg()
            print("已进入计划模式（只读，不写入；/approve 批准后执行）。" if ctx.plan_mode
                  else "已退出计划模式。"); continue
        if line == "/approve":
            if ctx.plan_mode:
                ctx.plan_mode = False
                messages[0] = _sys_msg()
                print("已批准，退出计划模式。说“继续/执行”让我落地计划。")
            else:
                print("当前不在计划模式。")
            continue
        if line == "/cost":
            print(meter.summary() if meter.turns else "本会话还没有模型调用。"); continue
        if line == "/raw":
            render_md = not render_md
            print(f"已切换为 {'原始流式' if not render_md else 'Markdown 渲染'} 输出。"); continue
        if line == "/compact":
            ak = cfg.get_active_key()
            if not ak:
                print("未配 key，无法压缩。"); continue
            before = sum(len(str(m.get('content') or '')) for m in messages)
            provider = from_settings(cfg.get_model_config(), ak)
            messages, summary = ctx_mod.compact(messages, provider)
            after = sum(len(str(m.get('content') or '')) for m in messages)
            if summary:
                memory.remember_summary(summary, sid)
            _persist()
            print(f"已压缩上下文（约 {before}→{after} 字），摘要已入库。" if summary else "上下文较短，无需压缩。")
            continue
        if line == "/init":
            p = memory.init_agents(str(cfg.IVYEA_DIR / "AGENTS.md"))
            if p[0]:
                print(f"已生成账户指令模板：{p[1]}\n填好后重开对话即自动注入。")
            else:
                print(f"已存在：{p[1]}（未覆盖）。`ivyea config edit` 或直接编辑它。")
            instructions = memory.load_instructions(os.getcwd())
            messages[0] = _sys_msg()
            continue
        if line == "/mcp":
            servers = cfg.load_mcp().get("mcpServers", {})
            print("MCP 服务器: " + (", ".join(servers) if servers else "(无，ivyea mcp add)")); continue
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
                print(f"未知模型 id：{mid}。用 /model 看清单。")
            continue
        if line.startswith("/"):
            hits = [c for c, _ in SLASH_COMMANDS if c.startswith(line.split()[0])]
            tip = ("，你是否想用：" + " ".join(hits)) if hits else "，输入 /help 看全部"
            print(f"未知命令 {line.split()[0]}{tip}"); continue

        # 自然语言 → Agent 循环
        api_key = cfg.get_active_key()
        if not api_key:
            print(f"⚠️ 未配置主脑模型 key，自然语言对话不可用。用 {_C['c']}/model{_C['x']} 选模型并配 key，或用斜杠命令。")
            continue
        messages.append({"role": "user", "content": line})
        try:
            mcfg = cfg.get_model_config()
            provider = from_settings(mcfg, api_key)
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
                print(f"{_C['d']}  (本轮 ¥{c:.4f} · 累计 ¥{meter.cost:.4f}){_C['x']}")
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
            # 自动压缩 + 摘要入库 + 落盘
            if ctx_mod.should_compact(int((out.get('usage') or {}).get('prompt_tokens') or 0)):
                messages, _s = ctx_mod.compact(messages, provider)
                if _s:
                    memory.remember_summary(_s, sid)
                    print(f"{_C['d']}（上下文较长，已自动压缩并入库摘要以省 token）{_C['x']}")
            _persist()
        except LLMError as e:
            print(f"\n[模型错误] {e}")
            messages.pop()  # 撤回这条 user，避免污染上下文


def _cmd_model(args: argparse.Namespace) -> int:
    from . import config as cfg, models
    cfg.ensure_dirs()
    if args.spec == "list":
        for group, items in models.grouped():
            print(group)
            for m in items:
                print(f"  {m['id']:<16} {m['label']}")
        return 0
    if args.spec:  # ivyea model <id>
        m = models.by_id(args.spec)
        if not m:
            print(f"未知模型 id：{args.spec}。`ivyea model list` 看清单，或 `ivyea model` 交互选。",
                  file=sys.stderr)
            return 2
        cfg.apply_model(m)
        print(f"已切换主脑: {m['label']}"
              f"（{'已配置 key' if cfg.get_active_key() else '未配置 key，运行 ivyea model 配置'}）")
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

    pm = sub.add_parser("mcp", help="MCP 配置/自检（add/list/remove/edit/tools/call）")
    pm.add_argument("action", choices=["add", "list", "remove", "edit", "tools", "call"])
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

    pa = sub.add_parser("apply", help="审核制执行巡检建议（默认 dry-run；--execute 才真写）")
    pa.add_argument("source", help="巡检输出目录 或 *明细*.csv 路径")
    pa.add_argument("--from-mcp", dest="from_mcp", help="执行用的 MCP 服务器（需配 writeActions）")
    pa.add_argument("--execute", action="store_true", help="真实执行（默认仅 dry-run 预览）")
    pa.add_argument("--protected", help="保护词清单，逗号分隔（这些词不否/不动）")
    pa.add_argument("--yes", action="store_true", help="跳过逐条确认，批准所有未被护栏拦截的动作")
    pa.set_defaults(func=_cmd_apply)

    plx = sub.add_parser("lingxing", help="领星 OpenAPI：setup / probe / sellers / operate <on|off|status>")
    plx.add_argument("action", choices=["setup", "probe", "sellers", "operate"])
    plx.add_argument("value", nargs="?", help="operate 的 on/off/status")
    plx.set_defaults(func=_cmd_lingxing)

    pu = sub.add_parser("audit", help="执行审计 / 回滚")
    pu.add_argument("action", choices=["list", "rollback"])
    pu.add_argument("id", nargs="?", help="rollback 的审计ID")
    pu.set_defaults(func=_cmd_audit)

    pmo = sub.add_parser("model", help="查看/配置主脑模型（交互；或 ivyea model deepseek:deepseek-chat）")
    pmo.add_argument("spec", nargs="?", help="provider:model，如 deepseek:deepseek-chat")
    pmo.set_defaults(func=_cmd_model)

    pmem = sub.add_parser("memory", help="记忆：status（默认）/ search <词> / note [asin]")
    pmem.add_argument("action", nargs="?", choices=["status", "search", "note"], default="status")
    pmem.add_argument("query", nargs="?")
    pmem.set_defaults(func=_cmd_memory)

    pch = sub.add_parser("chat", help="对话式 Agent（自然语言 + 斜杠命令 + 人工审批）")
    pch.add_argument("--from-mcp", dest="from_mcp", help="执行/拉数用的 MCP 服务器")
    pch.add_argument("--execute", action="store_true", help="允许真实写（默认 dry-run）")
    pch.add_argument("--protected", help="保护词清单，逗号分隔")
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
