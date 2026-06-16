"""Ivyea Agent CLI 入口。

P1 子命令：
  ivyea config show
  ivyea config set <key> <value>
  ivyea patrol <搜索词报告.csv> [--asin B0..] [--site US] [--target-acos 0.3] [--no-llm]
"""
from __future__ import annotations

import argparse
import sys

from . import __version__, config


def _cmd_config(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    if args.action == "show":
        s = config.load_settings()
        print(f"配置目录: {config.IVYEA_DIR}")
        print(f".env:      {config.ENV_FILE} ({'存在' if config.ENV_FILE.exists() else '缺失'})")
        print("settings:")
        for k, v in s.items():
            print(f"  {k} = {v}")
        provider = s.get("provider", "deepseek")
        print(f"主脑 key ({provider}): {'已配置' if config.get_api_key(provider) else '未配置'}")
        return 0
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

    pc = sub.add_parser("config", help="查看/设置配置")
    pc.add_argument("action", choices=["show", "set"])
    pc.add_argument("key", nargs="?")
    pc.add_argument("value", nargs="?")
    pc.set_defaults(func=_cmd_config)

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
