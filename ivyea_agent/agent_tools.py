"""对话式 Agent 的域工具注册表。

把已建原语暴露成 LLM 可调用的工具。写操作（execute_actions）在工具内部
强制走 permission 审批——LLM 无法绕过人工把关。
"""
from __future__ import annotations

import json
import socket
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from . import account_diagnosis, action_queue, actions as act_mod, competitor_audit, executor, guardrails, hooks, image_audit, knowledge, listing_audit, memory, ocr, offer_audit, permission, patrol as patrol_mod, profiles, review_audit, skills, tools_general
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
    session_id: str = ""                                   # 用于运行时间线
    turn_id: str = ""                                      # 当前用户轮次
    task_id: str = ""                                      # 绑定长任务，用于自动记录续跑/阻塞点
    ops_bridge: dict[str, Any] = field(default_factory=dict)  # IvyeaOps 嵌入模式工具桥接
    ops_context: dict[str, Any] = field(default_factory=dict)  # 当前 Ops 页面/板块上下文
    provider: Any = None                                       # 当前主脑 provider（供 dispatch_subagent）
    read_paths: set = field(default_factory=set)               # 本会话已 read_file 过的绝对路径（改前必读软护栏）


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
            "target_acos": {"type": "number", "description": "目标 ACOS；留空则读取运营画像/全局配置"},
            "days": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "run_account_diagnosis",
        "description": "账户级广告诊断：按 ASIN/活动/搜索词汇总浪费、赢家词、预算观察和 Listing 语义缺口。适合先看全局再决定是否巡检/执行。",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "搜索词/广告报表 CSV 路径"},
            "target_acos": {"type": "number", "description": "目标 ACOS，如 0.3"},
            "listing_text": {"type": "string", "description": "可选：Listing 标题/五点/A+ 文本，用于检查赢家词是否覆盖"},
            "min_clicks_no_order": {"type": "integer", "description": "零单浪费词最小点击数，默认 12"},
            "top_n": {"type": "integer", "description": "每组最多返回条数，默认 8"}},
            "required": ["source"]}}},
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
        "name": "knowledge_search",
        "description": "搜索 Ivyea 内置亚马逊知识库（官方摘要/规则卡/社区经验模板）。广告、Listing、预算、否词、关键词生命周期问题优先调用。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "skill_search",
        "description": "搜索 Ivyea 内置/用户 Skill（可复用运营流程）。复杂运营任务、周报、否词、预算、Listing、新品启动等优先调用。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "run_listing_audit",
        "description": "Listing 转化诊断：把广告搜索词/Review/价格信号映射到标题、五点、A+ 承接缺口。不要替代真实图片审核。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "bullets": {"type": "string"},
            "aplus": {"type": "string"},
            "search_terms": {"type": "array", "items": {"type": "string"}},
            "reviews": {"type": "string"},
            "price": {"type": "number"},
            "rating": {"type": "number"},
            "review_count": {"type": "integer"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "run_review_audit",
        "description": "Review/Q&A/Offer 归因：判断差评、评分、评论数、价格/coupon 是否导致广告低转化，避免误否相关词。",
        "parameters": {"type": "object", "properties": {
            "reviews": {"type": "string"},
            "qa": {"type": "string"},
            "rating": {"type": "number"},
            "review_count": {"type": "integer"},
            "price": {"type": "number"},
            "coupon": {"type": "string"},
            "competitor_price": {"type": "number"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "run_offer_audit",
        "description": "Offer/库存/利润诊断：根据售价、竞品价、毛利率、目标ACOS、库存天数、coupon、广告花费销售判断能否放量。",
        "parameters": {"type": "object", "properties": {
            "price": {"type": "number"},
            "competitor_price": {"type": "number"},
            "margin_rate": {"type": "number"},
            "target_acos": {"type": "number"},
            "inventory_days": {"type": "number"},
            "coupon": {"type": "string"},
            "spend": {"type": "number"},
            "sales": {"type": "number"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "run_competitor_audit",
        "description": "竞品/类目关键词诊断：识别竞品词、ASIN串号词、保护词、类目扩展和核心词缺口。",
        "parameters": {"type": "object", "properties": {
            "own_terms": {"type": "array", "items": {"type": "string"}},
            "search_terms": {"type": "array", "items": {"type": "string"}},
            "competitor_terms": {"type": "array", "items": {"type": "string"}},
            "category_terms": {"type": "array", "items": {"type": "string"}},
            "protected_terms": {"type": "array", "items": {"type": "string"}}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "run_image_audit",
        "description": "图片资产本地诊断：扫描 Listing 图片尺寸/比例/命名/缺图风险，并生成多模态大模型审核提示。",
        "parameters": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "product_context": {"type": "string"},
            "include_prompt": {"type": "boolean"}},
            "required": ["paths"]}}},
    {"type": "function", "function": {
        "name": "run_image_ocr",
        "description": "图片 OCR：使用本机 tesseract 识别图片文字；未安装时给出可操作提示。",
        "parameters": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "lang": {"type": "string", "description": "tesseract 语言，如 eng/chi_sim/eng+chi_sim"}},
            "required": ["paths"]}}},
    {"type": "function", "function": {
        "name": "recall",
        "description": "检索历史记忆(过往巡检、决策、记的要点)，跨会话回忆。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "ivyea_ops_list_tools",
        "description": "仅 IvyeaOps 嵌入模式可用：列出当前用户可调用的 IvyeaOps 板块工具，包括 Home、市场、Listing、广告审计、领星、资讯、监控等。",
        "parameters": {"type": "object", "properties": {
            "module": {"type": "string", "description": "可选：按板块过滤，如 home/market/listing/tools/lingxing/news/servmon/skill-hub"},
            "query": {"type": "string", "description": "可选：按名称或描述搜索"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "ivyea_ops_call_tool",
        "description": "仅 IvyeaOps 嵌入模式可用：调用一个 IvyeaOps 板块工具。写入/长任务会由 Ops 侧权限和工具策略控制。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "工具名，先用 ivyea_ops_list_tools 查看"},
            "arguments": {"type": "object", "description": "传给工具的 JSON 参数"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "dispatch_subagent",
        "description": "派一个只读子 agent 做聚焦调研/探索，返回其结论摘要。子 agent 只能用只读工具(grep/code_search/read_file/web_fetch/knowledge_search 等)、不能写、不能再派子 agent、步数受限。需要并行铺开多角度调研、或把一段独立的查证任务委派出去时用它，避免主线上下文被探索细节塞满。",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "交给子 agent 的具体问题/调研目标，越聚焦越好"},
            "max_steps": {"type": "integer", "description": "子 agent 工具步数上限，默认 12，最大 20"}},
            "required": ["task"]}}},
] + tools_general.GENERAL_TOOL_SCHEMAS


def _t_run_patrol(args: dict, ctx: ToolContext) -> str:
    asin = args.get("asin")
    profile = profiles.resolve(asin=asin or ctx.asin)
    site = args.get("site") or profile.get("site") or "US"
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
        from . import shadow
        shadow.record(args["sid"], result.get("candidates", []))   # 影子台账
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
        target_acos = args.get("target_acos")
        if target_acos is None:
            target_acos = profile.get("target_acos")
        res = patrol_mod.patrol(csv, asin=asin, site=site, target_acos=target_acos, use_llm=False)
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


def _t_run_account_diagnosis(args: dict, ctx: ToolContext) -> str:
    source = args.get("source") or ""
    if not source:
        return "错误：需要 source(CSV)。"
    profile = profiles.resolve(asin=args.get("asin") or ctx.asin)
    target_acos = args.get("target_acos")
    if target_acos is None:
        target_acos = profile.get("target_acos") or 0.3
    try:
        res = account_diagnosis.diagnose(
            source,
            target_acos=float(target_acos),
            listing_text=args.get("listing_text") or "",
            min_clicks_no_order=int(args.get("min_clicks_no_order") or 12),
            top_n=int(args.get("top_n") or 8),
        )
    except Exception as e:  # noqa: BLE001
        return f"账户诊断失败：{e}"
    return account_diagnosis.render_md(res)


def _t_propose_actions(args: dict, ctx: ToolContext) -> str:
    if not ctx.last_detail_csv:
        return "还没有巡检明细，请先 run_patrol。"
    profile = profiles.resolve(asin=ctx.asin)
    protected = list(ctx.protected) + list(profile.get("protected_terms") or [])
    acts = guardrails.annotate(act_mod.extract_actions(ctx.last_detail_csv, asin=ctx.asin),
                               protected_terms=protected)
    acts = memory.annotate(acts, ctx.asin)   # 记忆护栏：历史否决/5天稳定期
    ctx.actions = acts
    queued = action_queue.enqueue_actions(acts, source=ctx.last_detail_csv, origin="chat")
    ex = [a for a in acts if a.executable]
    bl = [a for a in acts if a.blocked]
    lines = [f"可执行 {len(ex)} 个，护栏拦截 {len(bl)} 个；新入队 {len(queued)} 个："]
    for a in ex:
        lines.append(f"  ✓ {a.summary()}（{a.term_category},{a.confidence}）")
    for a in bl:
        lines.append(f"  ✗ {a.summary()} — {a.block_reason}")
    lines.append("如需执行，调用 execute_actions（会逐条弹人工审批）。")
    return "\n".join(lines)


def _t_execute_lingxing(ctx: ToolContext) -> str:
    """领星候选：逐条人工审批 → 写入（默认 dry-run；真写需 operate 开关）。"""
    from . import lingxing_write as lw, shadow
    if shadow.shadow_mode():
        return "影子模式开：只记不写。建议已入台账，用 `ivyea shadow report` 看若照做的收益；关掉用 `ivyea shadow off`。"
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


def _t_knowledge_search(args: dict, ctx: ToolContext) -> str:
    return knowledge.render_search(args.get("query", ""), limit=int(args.get("limit") or 5))


def _t_skill_search(args: dict, ctx: ToolContext) -> str:
    return skills.render_search(args.get("query", ""), limit=int(args.get("limit") or 5))


def _t_run_listing_audit(args: dict, ctx: ToolContext) -> str:
    result = listing_audit.audit(
        title=args.get("title", ""),
        bullets=args.get("bullets", ""),
        aplus=args.get("aplus", ""),
        search_terms=args.get("search_terms") or [],
        reviews=args.get("reviews", ""),
        price=args.get("price"),
        rating=args.get("rating"),
        review_count=args.get("review_count"),
    )
    return listing_audit.render(result)


def _t_run_review_audit(args: dict, ctx: ToolContext) -> str:
    result = review_audit.audit(
        reviews=args.get("reviews", ""),
        qa=args.get("qa", ""),
        rating=args.get("rating"),
        review_count=args.get("review_count"),
        price=args.get("price"),
        coupon=args.get("coupon", ""),
        competitor_price=args.get("competitor_price"),
    )
    return review_audit.render(result)


def _t_run_offer_audit(args: dict, ctx: ToolContext) -> str:
    result = offer_audit.audit(
        price=args.get("price"),
        competitor_price=args.get("competitor_price"),
        margin_rate=args.get("margin_rate"),
        target_acos=args.get("target_acos"),
        inventory_days=args.get("inventory_days"),
        coupon=args.get("coupon", ""),
        spend=args.get("spend"),
        sales=args.get("sales"),
    )
    return offer_audit.render(result)


def _t_run_competitor_audit(args: dict, ctx: ToolContext) -> str:
    profile = profiles.resolve(asin=ctx.asin)
    result = competitor_audit.audit(
        own_terms=args.get("own_terms") or profile.get("core_terms") or [],
        search_terms=args.get("search_terms") or [],
        competitor_terms=args.get("competitor_terms") or profile.get("competitor_terms") or [],
        category_terms=args.get("category_terms") or profile.get("core_terms") or [],
        protected_terms=args.get("protected_terms") or profile.get("protected_terms") or ctx.protected or [],
    )
    return competitor_audit.render(result)


def _t_run_image_audit(args: dict, ctx: ToolContext) -> str:
    result = image_audit.audit(args.get("paths") or [])
    text = image_audit.render(result)
    if args.get("include_prompt"):
        text += "\n## 多模态审核 Prompt\n\n" + image_audit.multimodal_prompt(
            result, product_context=args.get("product_context") or ""
        ) + "\n"
    return text


def _t_run_image_ocr(args: dict, ctx: ToolContext) -> str:
    return ocr.render(ocr.run(args.get("paths") or [], lang=args.get("lang") or "eng"))


def _t_recall(args: dict, ctx: ToolContext) -> str:
    hits = memory.search(args.get("query", ""), limit=8)
    if not hits:
        return "（记忆里没有相关记录）"
    import time as _t
    return "\n".join(f"  · {_t.strftime('%m-%d', _t.localtime(h['ts']))} {h['text']}" for h in hits)


def _t_rollback(args: dict, ctx: ToolContext) -> str:
    r = executor.rollback(args.get("audit_id", ""))
    return r["detail"]


def _ops_bridge_request(ctx: ToolContext, path: str, payload: dict[str, Any], timeout: float = 80.0) -> dict[str, Any]:
    bridge = ctx.ops_bridge if isinstance(ctx.ops_bridge, dict) else {}
    base_url = str(bridge.get("base_url") or "").strip().rstrip("/")
    token = str(bridge.get("token") or "").strip()
    if not base_url or not token:
        return {
            "ok": False,
            "error": "ops_bridge_unavailable",
            "detail": "当前对话没有连接 IvyeaOps 工具桥。请在 IvyeaOps 右下角 IvyeaAgent 对话中使用。",
        }
    if "://" not in base_url:
        return {"ok": False, "error": "invalid_ops_bridge", "detail": "IvyeaOps 工具桥地址无效"}
    url = urllib.parse.urljoin(base_url + "/", path.lstrip("/"))
    body = dict(payload or {})
    if ctx.ops_context and "context" not in body:
        body["context"] = ctx.ops_context
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "IvyeaAgent-OpsBridge/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
            return data if isinstance(data, dict) else {"ok": False, "error": "invalid_response", "detail": "Ops 返回非对象 JSON"}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except OSError:
            detail = str(exc.reason)
        return {"ok": False, "error": f"HTTP {exc.code}", "detail": detail}
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": "ops_bridge_error", "detail": str(exc)}


def _compact_json_text(data: dict[str, Any], limit: int = 14000) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str, indent=2)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...（结果过长，已截断）"


def _t_ivyea_ops_list_tools(args: dict, ctx: ToolContext) -> str:
    data = _ops_bridge_request(ctx, "/tools", {
        "module": str(args.get("module") or ""),
        "query": str(args.get("query") or ""),
    }, timeout=20.0)
    return _compact_json_text(data, limit=12000)


def _t_ivyea_ops_call_tool(args: dict, ctx: ToolContext) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return "错误：需要提供工具名 name。"
    payload = {
        "name": name,
        "arguments": args.get("arguments") if isinstance(args.get("arguments"), dict) else {},
    }
    # 板块工具里有长任务（如生成市场调研/打法报告，要采集+AI合成），给宽限超时。
    data = _ops_bridge_request(ctx, "/call", payload, timeout=300.0)
    return _compact_json_text(data)


_DISPATCH = {
    "run_patrol": _t_run_patrol,
    "run_account_diagnosis": _t_run_account_diagnosis,
    "propose_actions": _t_propose_actions,
    "execute_actions": _t_execute_actions,
    "rollback": _t_rollback,
    "remember": _t_remember,
    "knowledge_search": _t_knowledge_search,
    "skill_search": _t_skill_search,
    "run_listing_audit": _t_run_listing_audit,
    "run_review_audit": _t_run_review_audit,
    "run_offer_audit": _t_run_offer_audit,
    "run_competitor_audit": _t_run_competitor_audit,
    "run_image_audit": _t_run_image_audit,
    "run_image_ocr": _t_run_image_ocr,
    "recall": _t_recall,
    "ivyea_ops_list_tools": _t_ivyea_ops_list_tools,
    "ivyea_ops_call_tool": _t_ivyea_ops_call_tool,
    **tools_general.GENERAL_DISPATCH,
}


# 可安全并行的只读工具：纯文件系统/网络读，不弹审批、不写共享状态（DB/索引文件）。
# 故意保守：DB 检索（knowledge/skill/recall）和会写索引文件的 code_search/symbols/impact
# 不在此列，避免 SQLite 跨线程或索引文件写竞争。
PARALLEL_SAFE = {"read_file", "list_dir", "web_fetch", "web_search", "grep", "glob",
                 "code_search", "code_symbols", "bash_output",
                 # 子 agent 只读且各自独立 sub_ctx/messages/PermissionState，可并行 fan-out。
                 # 前提：provider 实例无共享可变状态（openai_compat/anthropic/gemini 均为
                 # 每次调用独立请求）；若未来接入带会话状态的 provider 需复查。
                 "dispatch_subagent"}


@dataclass
class ToolResult:
    ok: bool
    text: str
    error: str = ""   # 完整 traceback，仅用于 trace/调试，不回灌给模型


def dispatch_result(name: str, args: dict, ctx: ToolContext) -> ToolResult:
    """派发工具并返回结构化结果：ok 反映是否抛异常（而非靠字符串猜），
    异常时 text 是给模型看的简短信息、error 保留 traceback 供排障。
    这里是全部工具（并行/串行/子 agent）的统一收口，pre/post_tool_use 钩子挂在此处；
    没配 hooks.json 时 enabled() 为 False，零开销。"""
    fn = _DISPATCH.get(name)
    if not fn:
        return ToolResult(False, f"未知工具：{name}")
    if hooks.enabled():
        allowed, reason = hooks.fire_decision(
            "pre_tool_use",
            {"tool_name": name, "tool_input": args or {},
             "session_id": getattr(ctx, "session_id", ""), "turn_id": getattr(ctx, "turn_id", "")},
            tool_name=name, readonly=name in READONLY_TOOLS)
        if not allowed:
            return ToolResult(False, f"pre_tool_use hook 拒绝：{reason}")
    try:
        res = ToolResult(True, fn(args or {}, ctx))
    except Exception as e:  # noqa: BLE001
        res = ToolResult(False, f"工具 {name} 执行出错：{e}", error=traceback.format_exc())
    if hooks.enabled():
        hooks.fire(
            "post_tool_use",
            {"tool_name": name, "tool_input": args or {}, "ok": res.ok,
             "tool_response": (res.text or "")[:2000],
             "session_id": getattr(ctx, "session_id", ""), "turn_id": getattr(ctx, "turn_id", "")},
            tool_name=name, readonly=name in READONLY_TOOLS)
    return res


def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    """字符串兼容入口（测试/只需文本的调用方用）。"""
    return dispatch_result(name, args, ctx).text


# 只读工具集：纯读/检索/审计/诊断，绝不写。供只读子 agent 使用。
# 注意剔除 dispatch_subagent：它虽只读可并行，但子 agent 不能递归再派子 agent。
READONLY_TOOLS = (PARALLEL_SAFE - {"dispatch_subagent"}) | {
    "code_search", "code_symbols", "code_impact", "code_repair",
    "mcp_list_tools", "mcp_list_resources", "mcp_read_resource",
    "mcp_list_prompts", "mcp_get_prompt",
    "knowledge_search", "skill_search", "recall",
    "run_patrol", "run_account_diagnosis", "propose_actions",
    "run_listing_audit", "run_review_audit", "run_offer_audit",
    "run_competitor_audit", "run_image_audit", "run_image_ocr",
    "task_read", "task_resume",
}


def _subagent_schemas() -> list:
    # dispatch_subagent 本身不在 READONLY_TOOLS 里 → 子 agent 不能再派子 agent。
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in READONLY_TOOLS]


def t_dispatch_subagent(args: dict, ctx: ToolContext) -> str:
    """跑一个只读子 agent 做聚焦调研，返回结论摘要（自带独立上下文，不污染主线）。"""
    task = (args.get("task") or "").strip()
    if not task:
        return "task 为空：描述要子 agent 查清的问题。"
    provider = getattr(ctx, "provider", None)
    if provider is None:
        return "当前环境无可用主脑 provider，无法派子 agent。"
    from . import agent_loop, config  # 延迟导入避免循环依赖
    try:
        _cap = int(config.get_setting("subagent_max_steps_cap", 40))
    except (TypeError, ValueError):
        _cap = 40
    max_steps = min(int(args.get("max_steps") or 12), max(1, _cap))
    sub_sys = ("你是只读调研子 agent。用只读工具(grep/code_search/read_file/web_fetch/knowledge_search 等)"
               "把交给你的问题查清楚，最后用简洁中文给出结论与依据(文件:行/来源)。"
               "你不能写文件、不能执行命令、不能改广告，也不要再派子 agent。")
    sub_ctx = ToolContext(workspace=getattr(ctx, "workspace", ""), plan_mode=True,
                          provider=provider, perm=permission.PermissionState())
    sub_messages = [{"role": "system", "content": sub_sys},
                    {"role": "user", "content": task}]
    try:
        result = agent_loop.run_turn(provider, sub_ctx, sub_messages, max_steps=max_steps,
                                     narrate=lambda s: None, tools=_subagent_schemas())
    except Exception as e:  # noqa: BLE001
        return f"子 agent 执行出错：{e}"
    return "【子 agent 结论】\n" + (result or "（无结论）")


_DISPATCH["dispatch_subagent"] = t_dispatch_subagent
