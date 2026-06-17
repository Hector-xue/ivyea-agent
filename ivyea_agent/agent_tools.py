"""对话式 Agent 的域工具注册表。

把已建原语暴露成 LLM 可调用的工具。写操作（execute_actions）在工具内部
强制走 permission 审批——LLM 无法绕过人工把关。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from . import actions as act_mod, executor, guardrails, memory, permission, patrol as patrol_mod, tools_general
from .rule_engine import RuleEngineError


@dataclass
class ToolContext:
    from_mcp: Optional[str] = None       # 执行/拉数用的 MCP 服务器
    execute: bool = False                # True=真写；False=dry-run
    protected: list = field(default_factory=list)
    last_report: str = ""
    last_detail_csv: str = ""
    asin: str = ""
    actions: list = field(default_factory=list)
    lingxing_result: dict = field(default_factory=dict)   # 最近一次领星巡检候选
    plan_mode: bool = False                                # 计划模式：禁止写入执行
    workspace: str = ""                                    # 通用工具的工作目录（默认 cwd）
    todos: list = field(default_factory=list)              # 当前任务计划（todo_write 维护）
    perm: permission.PermissionState = field(default_factory=permission.PermissionState)


# OpenAI function-calling schema
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "run_patrol",
        "description": "跑只读广告巡检。数据源三选一：本地 CSV(source)、MCP 服务器(from_mcp)、"
                       "或领星 OpenAPI 店铺维度(from_lingxing=true + sid，最真实，推荐)。",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "搜索词报告 CSV 路径（用 MCP/领星 时留空）"},
            "from_mcp": {"type": "string", "description": "MCP 服务器名（用 MCP 拉数时填）"},
            "from_lingxing": {"type": "boolean", "description": "true=走领星 OpenAPI 店铺维度规则引擎（需 sid）"},
            "sid": {"type": "integer", "description": "领星店铺 SID（from_lingxing 时必填）"},
            "asin": {"type": "string"}, "site": {"type": "string"},
            "days": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "propose_actions",
        "description": "基于最近一次巡检，提取可执行动作（否词/调价）并做护栏检查，返回动作清单。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "execute_actions",
        "description": "执行上一步提出的动作。每个写动作都会弹出人工审批（预览+确认），未经确认不会写。默认 dry-run。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "rollback",
        "description": "回滚一条审计记录的写操作。",
        "parameters": {"type": "object", "properties": {
            "audit_id": {"type": "string"}}, "required": ["audit_id"]}}},
    {"type": "function", "function": {
        "name": "remember",
        "description": "把一条值得长期记住的运营要点写入记忆(可按 ASIN 归档)。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "asin": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "recall",
        "description": "检索历史记忆(过往巡检、决策、记的要点)，跨会话回忆。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
] + tools_general.GENERAL_TOOL_SCHEMAS


def _t_run_patrol(args: dict, ctx: ToolContext) -> str:
    asin = args.get("asin")
    site = args.get("site") or "US"
    csv = args.get("source")
    if args.get("from_lingxing"):
        from . import lingxing_optimizer as opt, lingxing_report as lrep
        from .lingxing_openapi import LingXingError, is_configured
        if not is_configured():
            return "领星 OpenAPI 未配置（请先在终端 `ivyea lingxing setup`）。"
        if not args.get("sid"):
            return "走领星巡检需要 sid（用 `ivyea lingxing sellers` 查店铺）。"
        try:
            result = opt.run_store(int(args["sid"]), days=int(args.get("days", 30)))
        except LingXingError as e:
            return f"领星拉数失败：{e}"
        ctx.asin = f"sid:{args['sid']}"
        ctx.lingxing_result = result   # 供 execute_actions 写入
        return lrep.render(result, color=False)
    if args.get("from_mcp"):
        from .mcp_source import fetch_to_csv
        from .mcp_client import MCPError
        if not asin:
            return "错误：用 MCP 拉数需要 asin。"
        try:
            csv = fetch_to_csv(args["from_mcp"], asin, site, days=int(args.get("days", 30)))
        except MCPError as e:
            return f"MCP 拉数失败：{e}"
    if not csv:
        return "错误：需要 source(CSV) 或 from_mcp。"
    try:
        res = patrol_mod.patrol(csv, asin=asin, site=site, use_llm=False)
    except RuleEngineError as e:
        return f"规则引擎错误：{e}"
    ctx.last_report = res["text"]
    ro = res["rule_output"]
    ctx.last_detail_csv = ro.get("files", {}).get("details_csv", "")
    s = ro.get("summary", {})
    ctx.asin = s.get("asin") or asin or ""
    return (f"巡检完成 ASIN={s.get('asin')}。否词候选 {s.get('negative_candidate_count')}，"
            f"放量 {s.get('scale_up_count')}，控bid {s.get('reduce_bid_count')}。"
            f"报告已生成（{res['md_path']}）。可调用 propose_actions 看可执行动作。")


def _t_propose_actions(args: dict, ctx: ToolContext) -> str:
    if not ctx.last_detail_csv:
        return "还没有巡检明细，请先 run_patrol。"
    acts = guardrails.annotate(act_mod.extract_actions(ctx.last_detail_csv, asin=ctx.asin),
                               protected_terms=ctx.protected)
    acts = memory.annotate(acts, ctx.asin)   # 记忆护栏：历史否决/5天稳定期
    ctx.actions = acts
    ex = [a for a in acts if a.executable]
    bl = [a for a in acts if a.blocked]
    lines = [f"可执行 {len(ex)} 个，护栏拦截 {len(bl)} 个："]
    for a in ex:
        lines.append(f"  ✓ {a.summary()}（{a.term_category},{a.confidence}）")
    for a in bl:
        lines.append(f"  ✗ {a.summary()} — {a.block_reason}")
    lines.append("如需执行，调用 execute_actions（会逐条弹人工审批）。")
    return "\n".join(lines)


def _t_execute_lingxing(ctx: ToolContext) -> str:
    """领星候选：逐条人工审批 → 写入（默认 dry-run；真写需 operate 开关）。"""
    from . import lingxing_write as lw
    writable = []
    for c in ctx.lingxing_result.get("candidates", []):
        if c.get("blocked"):
            continue
        intent = lw.candidate_to_intent(c)
        if intent and intent.get("sid") is not None:
            writable.append(intent)
    if not writable:
        return "没有可写入的候选（收割为建议项、被拦截项不写）。"
    live = lw.operate_active()
    results = [f"可写 {len(writable)} 个；operate 开关：{'开（真实写入）' if live else '关（dry-run）'}。"]
    for intent in writable:
        decision = permission.request_intent(intent, lw.preview(intent), ctx.perm)
        if decision == permission.ABORT:
            results.append("用户终止。")
            break
        if decision == permission.DENY:
            memory.record_decision(f"sid:{intent.get('sid')}",
                                   intent.get("keyword_text") or str(intent.get("target_name")),
                                   lw._kind_for_memory(intent["op_type"]), "reject")
            results.append(f"跳过：{lw.preview(intent)}")
            continue
        r = lw.execute(intent, dry_run=not live)
        results.append(("✓ " if r["ok"] else "✗ ") + r["detail"])
    if not live:
        results.append("（dry-run 预览；真写需在终端 `ivyea lingxing operate on`。）")
    return "\n".join(results)


def _t_execute_actions(args: dict, ctx: ToolContext) -> str:
    if ctx.plan_mode:
        return "当前为计划模式（只读）：不执行写入。请先给用户行动计划，待 /approve 批准后再执行。"
    if ctx.lingxing_result:
        return _t_execute_lingxing(ctx)
    ex = [a for a in ctx.actions if a.executable]
    if not ex:
        return "没有可执行动作（先 propose_actions）。"
    if ctx.execute and not ctx.from_mcp:
        return "真实执行需要配置 from_mcp（含 writeActions）。当前可先 dry-run。"
    results = []
    for a in ex:
        decision = permission.request(a, ctx.perm)   # ← 人工审批，LLM 不能绕过
        if decision == permission.ABORT:
            results.append("用户终止。")
            break
        if decision == permission.DENY:
            memory.record_decision(ctx.asin, a.search_term, a.kind, "reject")
            results.append(f"跳过：{a.summary()}")
            continue
        memory.record_decision(ctx.asin, a.search_term, a.kind, "approve")
        r = executor.execute(a, ctx.from_mcp or "", dry_run=not ctx.execute)
        results.append(("✓ " if r["ok"] else "✗ ") + r["detail"])
    return "\n".join(results) if results else "无操作。"


def _t_remember(args: dict, ctx: ToolContext) -> str:
    return memory.remember(args.get("text", ""), args.get("asin") or ctx.asin)


def _t_recall(args: dict, ctx: ToolContext) -> str:
    hits = memory.search(args.get("query", ""), limit=8)
    if not hits:
        return "（记忆里没有相关记录）"
    import time as _t
    return "\n".join(f"  · {_t.strftime('%m-%d', _t.localtime(h['ts']))} {h['text']}" for h in hits)


def _t_rollback(args: dict, ctx: ToolContext) -> str:
    r = executor.rollback(args.get("audit_id", ""))
    return r["detail"]


_DISPATCH = {
    "run_patrol": _t_run_patrol,
    "propose_actions": _t_propose_actions,
    "execute_actions": _t_execute_actions,
    "rollback": _t_rollback,
    "remember": _t_remember,
    "recall": _t_recall,
    **tools_general.GENERAL_DISPATCH,
}


def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    fn = _DISPATCH.get(name)
    if not fn:
        return f"未知工具：{name}"
    try:
        return fn(args or {}, ctx)
    except Exception as e:  # noqa: BLE001
        return f"工具 {name} 执行出错：{e}"
