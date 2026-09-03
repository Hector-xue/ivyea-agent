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

from . import account_diagnosis, action_queue, actions as act_mod, competitor_audit, executor, guardrails, hooks, image_audit, knowledge, listing_audit, memory, memory_core, memory_store, ocr, offer_audit, permission, patrol as patrol_mod, profiles, review_audit, skills, tools_general
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
    workspace_declared: str = ""                           # 调用方显式指定的工作区；范围锁定不得越过它往上放宽
    todos: list = field(default_factory=list)              # 当前任务计划（todo_write 维护）
    perm: permission.PermissionState = field(default_factory=permission.PermissionState)
    session_id: str = ""                                   # 用于运行时间线
    turn_id: str = ""                                      # 当前用户轮次
    task_id: str = ""                                      # 绑定长任务，用于自动记录续跑/阻塞点
    # 本轮记忆写入连续失败了几次（熔断用）。记成 (turn_id, 次数)，
    # 换一轮自动归零 —— 不需要谁去主动 reset。
    memory_write_fails: tuple = ("", 0)
    # 本轮自动召回了哪几条记忆。运行时填、展示层读——**指示器必须是确定性的**：
    # 用户凭它知道"记忆起作用了"，而不是靠模型在回答里顺口提一句（模型经常不提）。
    memory_recall: dict[str, Any] = field(default_factory=dict)
    # 「拿不准就弹选项」的通道（ask.AskFn）。None = 没人可问（cron/飞书/管道），
    # 此时 ask_user_question 立刻按推荐项继续，不等 —— 见 ask.py 顶部。
    ask_fn: Any = None
    # 本轮有哪几项是**替用户定的**（超时/无通道按推荐项走）。收尾说明必须是
    # 确定性的：界面读这一份自己画，不能指望模型在总结里顺口提一句。
    auto_decisions: list = field(default_factory=list)
    asked_count: int = 0                                       # 本轮已经弹过几次选项（防问个不停）
    ops_bridge: dict[str, Any] = field(default_factory=dict)  # IvyeaOps 嵌入模式工具桥接
    ops_context: dict[str, Any] = field(default_factory=dict)  # 当前 Ops 页面/板块上下文
    provider: Any = None                                       # 当前主脑 provider（供 dispatch_subagent）
    read_paths: set = field(default_factory=set)               # 本会话已 read_file 过的绝对路径（改前必读软护栏）
    # 本轮改过的文件（工具写进来，agent_loop 排空后发成 file_change 事件）。
    # 放在 ctx 上是因为工具函数拿不到 emit —— 它只在 agent_loop 那一层。
    file_changes: list = field(default_factory=list)
    workspace_base: str = ""                                  # 会话最初工作区；跨仓库目标解析始终从这里发现候选
    target_project: str = ""                                  # 当前锁定的工程目标（跨轮保留，显式新目标可切换）
    target_root: str = ""
    target_explicit: bool = False
    scope_confidence: str = ""
    scope_ambiguous: bool = False
    behavioral_task: bool = False                              # UI/输出/交互类任务，完成前需运行路径验证
    search_recovery_required: bool = False                     # 0 文件后先 list_dir，禁止继续盲搜
    consecutive_search_deadends: int = 0
    navigation_since_read: int = 0
    executed_writes: bool = False                              # 本轮真的下过写指令（广告执行等），供收尾自查门禁判定
    route_lane: str = ""                                       # 本轮路线（routing.classify）：chat|board|work，供思考深度自适应
    thinking_effort: str = ""                                  # 本轮实际生效的思考深度（运行时填，展示层读）
    progress_reporting_disabled: bool = False                  # 只读子 agent 等内部执行不展示主任务汇报
    progress_required: bool = False                            # 复杂/多步任务启用结构化汇报闭环
    progress_execution_expected: bool = False                  # 用户明确要求落地执行，而非只要方案
    progress_query: str = ""
    progress_started: bool = False
    progress_start: dict[str, Any] = field(default_factory=dict)
    progress_active_phase: int = 0
    progress_started_phases: set[int] = field(default_factory=set)
    progress_phase_reports: dict[int, dict[str, Any]] = field(default_factory=dict)
    progress_final: dict[str, Any] = field(default_factory=dict)
    progress_tool_evidence: list[str] = field(default_factory=list)
    progress_phase_tool_evidence: dict[int, list[str]] = field(default_factory=dict)
    progress_attention: list[str] = field(default_factory=list)
    progress_last_event: dict[str, Any] = field(default_factory=dict)
    knowledge_citations: list[dict[str, Any]] = field(default_factory=list)  # 本轮可用的 [K#] 证据
    knowledge_retrieval_expected: bool = False                              # 命中亚马逊检索路由
    knowledge_risk: str = "none"                                           # none/low/medium/high
    knowledge_query: str = ""
    # 本轮带图请求实际走了视觉三档中的哪一档（见 vision.route_images）。
    # 必须一路回传到前端/IvyeaOps：降级本身不是问题，**降级了却不说**才是——
    # 此前正是因为没人知道降级发生过，Listing 的图片分析静默空转了很久。
    vision_tier: dict[str, Any] = field(default_factory=dict)
    vision_notes: list[str] = field(default_factory=list)
    # ── 目标模式 ────────────────────────────────────────────────────────────
    # 「一句话交出去，达成之前不停」。开关本身在这里，验收标准落在 goal_store
    # （跨上下文压缩不丢），判定由 agent_loop._goal_gate_feedback 做 —— 三样东西
    # 都在运行时手上，模型改不了自己的及格线。
    goal_mode: bool = False
    goal_query: str = ""                                       # 立约时用的那句指令（换指令要重新立约）
    goal_state: dict[str, Any] = field(default_factory=dict)   # 给界面的确定性投影（goal_store.public_state）
    # 交给验收员看的证据。**必须带命令原文和输出**：`progress_tool_evidence` 每条只留
    # 结果的第一行，而 run_command 的第一行恰好是「[退出码 0]」—— 验收员因此看不到
    # 跑的是哪条命令、输出是什么，只能一遍遍判"只有声称、无证据"（真机冒烟实测，
    # 连判三轮相同后撞上无进展熔断）。判定的上限是证据通道的上限。
    goal_evidence: list[str] = field(default_factory=list)


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
        "description": "跨会话统一回忆：一次查遍分类记忆、历史记录（巡检/决策/对话），"
                       "并列出相关的知识卡与 Skill 指针。想不起来「上次怎么处理的」就用它。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "memory_write",
        "description": ("把一件值得长期记住的事写成一条**分类记忆文件**（一事一文件）。"
                        "只记：用户的问题/指令要点、关键过程、最终结论——不要把整段对话抄进去。"
                        "四种操作：add 新建（会自动查重，撞上相近记忆会让你改用 update）；"
                        "update 覆盖同名记忆的正文（事实变了、有新结论就更新，别新建第二条）；"
                        "delete 删掉已被推翻的记忆；noop 表示本次没有值得沉淀的东西。"
                        "默认合并优先于新建——同一件事分裂成多条会让记忆越用越碎。"
                        f"分类：{'; '.join(f'{k}={v}' for k, v in memory_store.CATEGORIES.items())}"),
        "parameters": {"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["add", "update", "delete", "noop"]},
            "name": {"type": "string", "description": "记忆名，要让人能开口叫出来，如「领星广告方法论」"},
            "content": {"type": "string", "description": "记忆正文：问题/指令、关键过程、结论"},
            "category": {"type": "string", "enum": list(memory_store.CATEGORIES)},
            "description": {"type": "string", "description": "一句话描述，会进索引层供日后判断相关性"},
            "keywords": {"type": "string", "description": "逗号分隔的关键词，帮助日后检索"},
            "links": {"type": "string", "description": "关联的其它记忆名，写成 [[名字]] 形式"},
            "scope": {"type": "string",
                      "description": "作用域：这条只对某个店铺/ASIN 成立时填，如 store:US主店 或 asin:B08XXX。"
                                     "全局适用就留空。填错会让别的店铺读到不该用的规则。"},
            "valid_from": {"type": "string", "description": "事实生效日期 YYYY-MM-DD，如「双十一起改成…」"},
            "valid_until": {"type": "string",
                            "description": "事实失效日期 YYYY-MM-DD。**已知是临时的就一定要填**"
                                           "（如旺季阈值、促销期规则），到期自动不再进检索，"
                                           "省得日后拿过期规则误导决策"}},
            "required": ["operation"]}}},
    {"type": "function", "function": {
        "name": "memory_search",
        "description": "在分类记忆里检索相关条目，返回名字/描述/正文。你的上下文里已有记忆索引目录，"
                       "先看目录判断哪条相关，需要正文时用这个或 memory_read 取。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "memory_read",
        "description": "按名字读取一条分类记忆的完整正文。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "category": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "core_memory_view",
        "description": "查看核心记忆块的当前内容。核心记忆每轮都在你的上下文里，改之前先看一眼，"
                       "避免重复写入或改错位置。",
        "parameters": {"type": "object", "properties": {
            "block": {"type": "string", "enum": ["user", "agents"],
                      "description": "user=用户画像；agents=账户打法与边界"}},
            "required": ["block"]}}},
    {"type": "function", "function": {
        "name": "core_memory_edit",
        "description": ("更新核心记忆——**关于用户本人或长期打法的事实**，写进去以后每轮常驻你的上下文，"
                        "不需要检索就一直知道。什么时候用：用户表达了长期偏好('以后都用中文汇报')、"
                        "定下了长期规则('品牌词永远不否')、纠正了你的做法、或透露了稳定的身份/目标信息。"
                        "什么时候**不要**用：一次性的任务细节、某个 ASIN 的具体数据、会过期的临时状态——"
                        "那些用 remember 写进普通记忆即可。"
                        f"块：{'; '.join(k + '=' + v[1] for k, v in memory_core.BLOCKS.items())}"),
        "parameters": {"type": "object", "properties": {
            "block": {"type": "string", "enum": ["user", "agents"]},
            "operation": {"type": "string", "enum": ["append", "replace", "remove"],
                          "description": "append=追加一条；replace=把 old 精确换成 content；remove=删掉含 old 的行"},
            "content": {"type": "string", "description": "append/replace 的新内容"},
            "old": {"type": "string", "description": "replace/remove 的原文，必须唯一命中"}},
            "required": ["block", "operation"]}}},
    {"type": "function", "function": {
        "name": "show_image",
        "description": "把一张**已经存在的**图片文件展示给用户看。你截的图、跑出来的图表、读到的产品图，想让用户亲眼看看就用它 —— 光用文字描述用户是看不见的。在 IvyeaOps 网页里会直接渲染成图（这时要把返回的 `![说明](地址)` 原样写进你的回答正文）；在终端里不渲染画面，只报路径。作图请用 image_generate，这个工具只负责展示已有文件。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "图片文件路径"},
            "caption": {"type": "string", "description": "一句话说明，会变成图的 alt 文字"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "ivyea_ops_list_tools",
        "description": "仅 IvyeaOps 嵌入模式可用：列出当前用户可调用的 IvyeaOps 板块工具，包括 Home、市场、Listing、广告审计、领星、资讯、监控，**以及 AI 作图（image_generate）**。用户提出作图/出图/画一张/生成主图之类的需求时，先用这个查一下 —— 宿主机器上通常已经配好了生图链路，别直接回答\"我没有图像生成能力\"。",
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
        "description": "派一个只读子 agent 做聚焦调研/探索，返回其结论摘要。用 role 选分工（默认通用调研）。子 agent 只能用只读工具、不能写、不能执行命令、不能再派子 agent、步数受限，且有自己的预算不吃主线额度。需要并行铺开多角度调研、或把一段独立的查证任务委派出去时用它，避免主线上下文被探索细节塞满。同一步里可以派多个不同角色并行。",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "交给子 agent 的具体问题/调研目标，越聚焦越好"},
            "role": {"type": "string",
                     "enum": ["researcher", "code_explorer", "data_analyst",
                              "listing_auditor", "ads_reviewer", "knowledge_auditor"],
                     "description": "分工角色：researcher=通用调研(默认)；code_explorer=在代码库里定位"
                                    "实现/调用方/影响面；data_analyst=拉取并解读账户广告数据；"
                                    "listing_auditor=Listing/评论/图片审核；ads_reviewer=复核广告动作"
                                    "(数据够不够、护栏有没有越)；knowledge_auditor=核对事实来源与适用范围。"
                                    "用户在 ~/.ivyea/agents/*.md 里自定义的角色名也可以填。"},
            "max_steps": {"type": "integer", "description": "子 agent 工具步数上限；不填按角色默认"}},
            "required": ["task"]}}},
    {"type": "function", "function": {
        "name": "ask_user_question",
        "description": "方案有分叉、且不同选法会做出不同的东西时，把选项弹给用户自己选（工作台会弹一张选项卡）。"
                       "用在真正拿不准的地方：技术路线二选一、要不要动某个已有行为、范围到哪为止。"
                       "**不要**用它问那些看代码/看数据就能自己确定的事，也不要用来确认"
                       "「我可以开始了吗」。每轮最多 3 次。给每个选项写清楚选它会发生什么，"
                       "并把你自己推荐的那个标 recommended=true —— 用户 5 分钟不选就按推荐项继续，"
                       "那时收尾总结里必须说明这一项是自动定的。终端里多选会退化成单选。",
        "parameters": {"type": "object", "properties": {
            "questions": {"type": "array", "description": "1-4 个问题；每个问题 2-4 个选项",
                          "items": {"type": "object", "properties": {
                              "question": {"type": "string", "description": "完整的问句"},
                              "header": {"type": "string", "description": "≤12 字的短标签，如「投递语义」"},
                              "multi_select": {"type": "boolean", "description": "true=可多选"},
                              "options": {"type": "array", "items": {"type": "object", "properties": {
                                  "label": {"type": "string", "description": "选项名（1-6 个词）"},
                                  "description": {"type": "string", "description": "选它意味着什么、代价是什么"},
                                  "recommended": {"type": "boolean", "description": "你推荐的那一项标 true（只能标一个）"}},
                                  "required": ["label"]}}},
                              "required": ["question", "options"]}}},
            "required": ["questions"]}}},
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
        if live and r.get("ok"):
            ctx.executed_writes = True   # 真下过写指令 → 收尾前必须走一次自查门禁
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
        if ctx.execute and r.get("ok"):
            ctx.executed_writes = True   # 真下过写指令 → 收尾前必须走一次自查门禁
        results.append(("✓ " if r["ok"] else "✗ ") + r["detail"])
    return "\n".join(results) if results else "无操作。"


def _t_remember(args: dict, ctx: ToolContext) -> str:
    return memory.remember(args.get("text", ""), args.get("asin") or ctx.asin)


# 同一轮里记忆写入连续失败几次就停手。
#
# **记忆是副作用，副作用失败绝不能吃掉用户的回答。** 没有这道闸的话，
# 一次查重冲突或超上限会让模型反复重试同一个写入，把这一轮的工具预算耗光，
# 用户等了半天什么都没等到——而它本来只是想顺手记一笔。
MEMORY_WRITE_FAIL_LIMIT = 3

_MEMORY_CIRCUIT_MSG = (
    "记忆写入在这一轮已经连续失败 {n} 次。**别再重试了**——先把回答给用户，"
    "这条以后再存。"
)


def _memory_write_outcome(ctx: ToolContext, ok: bool, message: str) -> str:
    """记一次记忆写入的成败，必要时熔断。返回给模型看的文本。"""
    turn = str(getattr(ctx, "turn_id", "") or "")
    prev_turn, n = getattr(ctx, "memory_write_fails", ("", 0)) or ("", 0)
    n = int(n) if prev_turn == turn else 0        # 换轮归零
    if ok:
        ctx.memory_write_fails = (turn, 0)        # 成功即清零：熔断数的是**连续**失败
        return message
    n += 1
    ctx.memory_write_fails = (turn, n)
    if n >= MEMORY_WRITE_FAIL_LIMIT:
        return _MEMORY_CIRCUIT_MSG.format(n=n)
    return message


def _t_memory_write(args: dict, ctx: ToolContext) -> str:
    res = memory_store.apply(
        (args.get("operation") or "").strip(),
        name=args.get("name") or "",
        content=args.get("content") or "",
        category=args.get("category") or "",
        description=args.get("description") or "",
        keywords=args.get("keywords") or "",
        links=args.get("links") or "",
        scope=args.get("scope") or "",
        valid_from=args.get("valid_from") or "",
        valid_until=args.get("valid_until") or "",
    )
    return _memory_write_outcome(ctx, bool(res.get("ok")), res.get("message", ""))


def _t_memory_search(args: dict, ctx: ToolContext) -> str:
    hits = memory_store.search(args.get("query", ""), limit=int(args.get("limit") or 8))
    if not hits:
        return "（分类记忆里没有相关条目）"
    # 联想：把命中记忆显式链接到的那几条一并带出来（限 1 跳、限条数）。
    # 这些是作者写下的"这两件事相关"，往往正是回答问题真正需要的那一半。
    linked = memory_store.expand_linked([memory_store.get(h["name"], h["category"]) for h in hits])
    extra = [e.to_dict() for e in linked[len(hits):]]
    out = []
    for h in hits + extra:
        # 正文截断：检索结果是给模型判断"要不要细看"的，要全文用 memory_read
        body = h["body"].strip()
        if len(body) > 800:
            body = body[:800] + f"\n…（正文共 {len(h['body'])} 字，用 memory_read 取全文）"
        tag = f"（相关度 {h['score']}）" if h.get("score") else "（关联带出）"
        note = " ⚠推断" if h.get("confidence", 1.0) < memory_store.UNCERTAIN_BELOW else ""
        out.append(f"### [{h['category']}/{h['name']}]{tag}{note}\n"
                   f"{h['description']}\n{body}")
    return "\n\n".join(out)


def _t_memory_read(args: dict, ctx: ToolContext) -> str:
    e = memory_store.get(args.get("name", ""), args.get("category") or "")
    if not e:
        return f"没有找到名为 {args.get('name')!r} 的记忆。用 memory_search 查一下确切名字。"
    return f"### [{e.category}/{e.name}]\n{e.description}\n\n{e.body}"


def _t_core_memory_view(args: dict, ctx: ToolContext) -> str:
    block = (args.get("block") or "").strip()
    if block not in memory_core.BLOCKS:
        return f"未知的记忆块 {block!r}，可选：{', '.join(memory_core.BLOCKS)}"
    text = memory_core.view(block)
    if not text.strip():
        return f"{memory_core.BLOCKS[block][0]} 目前是空的。"
    return text


def _t_core_memory_edit(args: dict, ctx: ToolContext) -> str:
    res = memory_core.edit(
        (args.get("block") or "").strip(),
        (args.get("operation") or "").strip(),
        args.get("content") or "",
        args.get("old") or "",
    )
    # 漂移拒绝**不计入熔断**：那是"你该重新 view 一遍再写"，正是我们希望模型去做的事，
    # 把它算成失败会让第三次重试被熔断掉，反而拦住了正确的补救动作。
    if res.get("drift"):
        return res.get("message", "")
    return _memory_write_outcome(ctx, bool(res.get("ok")), res.get("message", ""))


def _t_knowledge_search(args: dict, ctx: ToolContext) -> str:
    query = str(args.get("query") or "")
    evidence = knowledge.evidence_context(query, limit=int(args.get("limit") or 5))
    merged, text = knowledge.merge_citations(
        list(ctx.knowledge_citations or []),
        list(evidence.get("citations") or []),
        str(evidence.get("text") or ""),
    )
    ctx.knowledge_citations = merged
    ctx.knowledge_retrieval_expected = bool(evidence.get("should_retrieve"))
    ctx.knowledge_risk = str(evidence.get("risk") or "none")
    ctx.knowledge_query = query
    return text or "（该问题未触发亚马逊知识检索）"


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
    """统一回忆：分类记忆（提炼过的）→ 情景记忆（原始片段）→ 知识/Skill 指针。

    知识卡和 Skill 只回**指针**（标题 + 取用工具），不回正文：knowledge 的正文必须经
    knowledge_search → evidence_context 走一遍，才会登记引证键；这里直接把正文吐出来，
    模型就会拿着没登记的 [K?] 键去标注结论，引证契约当场作废。指针既打通了"知道有这个"，
    又不绕过登记。
    """
    query = str(args.get("query") or "")
    blocks: list[str] = []

    # 检索本身走 memory.recall_core —— **每轮自动召回用的是同一个函数**。
    # 两条路各写一份的话早晚漂移，而漂移的那条不会有人发现，直到某天发现
    # "工具查得到、自动召回查不到"。
    # record=True：这是用户/模型主动发起的一次回忆，算作"这条记忆被用到了";
    # 自动召回那边则必须 record=False，否则每轮都跑会把遗忘打分刷成一片热门。
    core = memory.recall_core(query, limit=4, episodes=6, record=True)

    # 1) 分类记忆优先：它是提炼过的结论，比原始对话片段密度高得多
    curated = core["curated"]
    if curated:
        blocks.append("【分类记忆】（用 memory_read 取全文）\n" + "\n".join(
            f"  · [{h['category']}/{h['name']}] {h['description'] or h['body'][:60]}" for h in curated))

    # 2) 情景记忆：原始片段，用于"上次聊到的那个…"这类模糊回忆
    hits = core["episodes"]
    if hits:
        import time as _t
        blocks.append("【历史记录】\n" + "\n".join(
            f"  · {_t.strftime('%m-%d', _t.localtime(h['ts']))} {h['text'][:160]}" for h in hits))

    # 3) 知识 / Skill：只给指针
    try:
        cards = knowledge.search(query, limit=3)
        if cards:
            blocks.append("【相关知识卡】（要用作依据必须调 knowledge_search 取，才有引证键）\n"
                          + "\n".join(f"  · {c['title']}（{c['id']}）" for c in cards))
    except Exception:  # noqa: BLE001 —— 知识库缺失不该让回忆整个失败
        pass
    try:
        # **纯词法**：这里的技能指针是要注进回答里的，和自动注入同一个风险类别。
        # 语义在小语料（几十条技能）上没有"都不像"这个答案 —— 余弦总会给出最像的那几条，
        # 于是"完全不存在的东西"也能匹配出三条技能，正是"不管问什么第一句都在匹配技能"
        # 那个老事故的语义版。想按语义找技能是 skill_search 的事，那里用户是在主动翻库。
        found = skills.search(query, limit=3, semantic=False)
        if found:
            blocks.append("【相关 Skill】（用 skill_search 取流程）\n"
                          + "\n".join(f"  · {sk.title}（{sk.id}）" for sk, _ in found))
    except Exception:  # noqa: BLE001
        pass

    return "\n\n".join(blocks) if blocks else "（记忆里没有相关记录）"


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


def _ops_tool_catalog(ctx: ToolContext) -> dict[str, dict[str, Any]]:
    """板块能力目录（name → 元数据），按 ctx 缓存一次。

    目录里每条自带中文 title 和 destructive 标记，是审批卡文案和"要不要拦"的
    判断依据。一轮里可能调好几个板块工具，没必要每次都去问一遍。
    """
    cached = getattr(ctx, "_ops_tool_catalog", None)
    if cached is not None:
        return cached
    catalog: dict[str, dict[str, Any]] = {}
    data = _ops_bridge_request(ctx, "/tools", {}, timeout=20.0)
    for row in (data.get("tools") or []):
        if isinstance(row, dict) and row.get("name"):
            catalog[str(row["name"])] = row
    try:
        ctx._ops_tool_catalog = catalog          # noqa: SLF001 — 就近缓存，随 ctx 生灭
    except Exception:  # noqa: BLE001
        pass
    return catalog


#: show_image 认得的图片魔数。**按文件头判，不看扩展名** —— 扩展名是模型说了算的。
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"), (b"GIF89a", "gif"), (b"BM", "bmp"),
)


def _sniff_image_ext(head: bytes) -> str:
    for magic, ext in _IMAGE_MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return "avif"
    return ""


def _t_show_image(args: dict, ctx: ToolContext) -> str:
    """把一张图给用户看。**两种宿主，两种结果，但只有一个工具名。**

    · 嵌进 IvyeaOps（serve）→ 委托给 ops 的同名工具。图片本体不进模型：ops 读文件、
      复制进会话图库、回一个站内地址，模型把 `![](地址)` 写进正文，网页上就渲染成图。
      **权限边界也在 ops 那边**（只读已绑定目录的工作区），这边不自己开一套。
    · 终端（CLI）→ 没有浏览器可送，也没有出口可挂，所以只做一件诚实的事：确认这
      确实是一张图、把绝对路径报回去。用户在终端里看不到画面，但至少知道是哪张图 ——
      这正是用户描述的形态（"虽然在 CLI 端不会显示"）。

    **绝不往 stdout 打字**：`-p --output-format stream-json` 下 stdout 是协议通道，
    多一行就把调用方的解析打断了。要说的话一律走返回值。
    """
    raw = str(args.get("path") or "").strip()
    caption = str(args.get("caption") or "").strip()
    if not raw:
        return "错误：需要提供 path（图片文件路径）。"

    bridge = ctx.ops_bridge if isinstance(ctx.ops_bridge, dict) else {}
    if bridge.get("base_url"):
        data = _ops_bridge_request(
            ctx, "/call", {"name": "show_image",
                           "arguments": {"path": raw, "caption": caption}}, timeout=60.0)
        return _compact_json_text(data)

    # —— CLI ——
    from pathlib import Path as _P
    try:
        target = _P(raw).expanduser().resolve()
    except OSError as exc:
        return f"错误：路径解不开：{exc}"
    if not target.is_file():
        return f"错误：{target} 不是一个文件（或不存在）。"
    try:
        with open(target, "rb") as fh:
            head = fh.read(16)
        size = target.stat().st_size
    except OSError as exc:
        return f"错误：读不了这个文件：{exc}"
    ext = _sniff_image_ext(head)
    if not ext:
        return (f"错误：{target} 不是图片（按文件头判定；支持 png/jpg/gif/webp/bmp/avif）。")
    return (f"[图片] {target}（{ext}，{size} 字节）"
            + (f" —— {caption}" if caption else "")
            + "\n终端里不渲染画面，把这个路径告诉用户即可；他在网页端（IvyeaOps 任务台）"
              "问同一件事时，同一个工具会把图直接显示出来。")


def _t_ivyea_ops_call_tool(args: dict, ctx: ToolContext) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return "错误：需要提供工具名 name。"
    arguments = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}

    # 写类板块能力（建项目、启动审计、开领星可写开关…）在**有人可问**的时候必须
    # 先过审批。只在接了审批通道（serve 的远程确认卡）时才拦：没有通道就说明
    # 没人能确认，此时保持既有行为不变——嵌入式对话一直是这么跑的，这里不改。
    if ctx.perm.prompt_fn is not None:
        meta = _ops_tool_catalog(ctx).get(name) or {}
        if meta.get("destructive"):
            title = str(meta.get("title") or name)
            preview_lines = [f"调用板块能力：{title}（{name}）"]
            for key, val in list(arguments.items())[:8]:
                preview_lines.append(f"- {key}: {str(val)[:120]}")
            decision = permission.request_intent(
                {"op_type": "ops_tool_call", "tool": name},
                "\n".join(preview_lines), ctx.perm,
            )
            if decision != permission.APPROVE:
                return f"已取消：用户未批准调用板块能力「{title}」。"

    payload = {"name": name, "arguments": arguments}
    # 板块工具里有长任务（如生成市场调研/打法报告，要采集+AI合成），给宽限超时。
    data = _ops_bridge_request(ctx, "/call", payload, timeout=300.0)
    return _compact_json_text(data)


def _t_ask_user_question(args: dict, ctx: ToolContext) -> str:
    """把选项弹给用户，等他选；没人选就按推荐项继续，并记账。

    **一定会返回一份答案**：这个工具的意义是让一轮任务在分叉处不卡死，而不是
    多一个可能挂住的地方。没有通道（cron/飞书/管道）就立刻按推荐项走，一秒不等。
    """
    from . import ask as ask_mod

    try:
        questions = ask_mod.normalize(args.get("questions"))
    except ValueError as exc:
        return f"参数不对：{exc}。请修正后重试。"

    asked = int(getattr(ctx, "asked_count", 0) or 0)
    if asked >= ask_mod.MAX_ASKS_PER_TURN:
        picks = ask_mod.recommended_answers(questions)
        return ("这一轮已经问过 " + str(asked) + " 次了，不再打扰用户。"
                "按你自己推荐的选项继续：" + _format_answers(picks)
                + "\n收尾时说明这几项是你自行决定的。")
    ctx.asked_count = asked + 1

    timeout = _ask_timeout()
    out = ask_mod.resolve(questions, getattr(ctx, "ask_fn", None), timeout)
    answers = out.get("answers") or {}
    reason = str(out.get("reason") or "")
    # 记账按**问**记，不按整次调用记：一次问四问、人只点了两问，剩下两问同样是
    # "替他定的"，一样要出现在收尾说明里。
    filled = set(out.get("auto_filled") or [])
    by_text = {q["question"]: q for q in questions}
    for question in filled:
        q = by_text.get(question, {})
        ctx.auto_decisions.append({
            "question": question,
            "header": q.get("header") or "",
            "chosen": answers.get(question, ""),
            "reason": reason,
        })
    if out.get("auto"):
        why = {
            "timeout": f"用户在 {int(timeout // 60)} 分钟内没有选择",
            # 跳过是用户**主动放权**，不是没人理 —— 说成"没有选择"就是冤枉他。
            "skipped": "用户跳过了这些问题，把决定权交给你",
            "no_channel": "当前没有可以弹选项的界面（无人值守运行）",
            "error": "提问通道出错",
        }.get(reason, "没能拿到用户的选择")
        return (f"{why}，已按你标记的推荐项继续：{_format_answers(answers)}\n"
                "**最终总结里必须明确说明这几项是自动决定的、依据是什么、"
                "以及用户如果想改该怎么改。**")
    if filled:
        picked = {k: v for k, v in answers.items() if k not in filled}
        return ("用户选择：" + _format_answers(picked)
                + "\n其余几问他没选，已按推荐项继续："
                + _format_answers({k: answers[k] for k in filled if k in answers})
                + "\n**最终总结里要说明后面这几项是自动定的。**")
    return "用户选择：" + _format_answers(answers)


def _format_answers(answers: dict) -> str:
    return "；".join(f"「{q}」→ {a}" for q, a in answers.items()) or "（无）"


def _ask_timeout() -> float:
    from . import ask as ask_mod, config as cfg_mod
    try:
        val = float(cfg_mod.get_setting("ask_timeout_seconds", 0) or 0)
    except (TypeError, ValueError):
        val = 0.0
    return val if val > 0 else ask_mod.DEFAULT_ASK_TIMEOUT


_DISPATCH = {
    "ask_user_question": _t_ask_user_question,
    "run_patrol": _t_run_patrol,
    "run_account_diagnosis": _t_run_account_diagnosis,
    "propose_actions": _t_propose_actions,
    "execute_actions": _t_execute_actions,
    "rollback": _t_rollback,
    "remember": _t_remember,
    "memory_write": _t_memory_write,
    "memory_search": _t_memory_search,
    "memory_read": _t_memory_read,
    "core_memory_view": _t_core_memory_view,
    "core_memory_edit": _t_core_memory_edit,
    "knowledge_search": _t_knowledge_search,
    "skill_search": _t_skill_search,
    "run_listing_audit": _t_run_listing_audit,
    "run_review_audit": _t_run_review_audit,
    "run_offer_audit": _t_run_offer_audit,
    "run_competitor_audit": _t_run_competitor_audit,
    "run_image_audit": _t_run_image_audit,
    "run_image_ocr": _t_run_image_ocr,
    "recall": _t_recall,
    "show_image": _t_show_image,
    "ivyea_ops_list_tools": _t_ivyea_ops_list_tools,
    "ivyea_ops_call_tool": _t_ivyea_ops_call_tool,
    **tools_general.GENERAL_DISPATCH,
}


# 可安全并行的只读工具：纯文件系统/网络读，不弹审批、不写共享状态（DB/索引文件）。
# 故意保守：DB 检索（knowledge/skill/recall）和会写索引文件的 code_search/symbols/impact
# 不在此列，避免 SQLite 跨线程或索引文件写竞争。
PARALLEL_SAFE = {"read_file", "list_dir", "web_fetch", "web_search", "web_images", "grep", "glob",
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
    # core_memory_view 只读文件；core_memory_edit 故意**不**进只读集：
    # 子 agent 不该改主人的长期画像，那是主线才有权做的决定。
    "knowledge_search", "skill_search", "recall", "self_critique", "core_memory_view",
    # skill_view 只读技能全文/附属文件；skill_write 故意**不**进只读集 ——
    # 子 agent 的一次探索不足以决定"这值得沉淀成技能"，那是主线的判断。
    "skill_view",
    # memory_search/read 只读；memory_write 不进——子 agent 的探索结论该由主线判断要不要沉淀
    "memory_search", "memory_read",
    "run_patrol", "run_account_diagnosis", "propose_actions",
    "run_listing_audit", "run_review_audit", "run_offer_audit",
    "run_competitor_audit", "run_image_audit", "run_image_ocr",
    "task_read", "task_resume",
}


def _subagent_schemas() -> list:
    # dispatch_subagent 本身不在 READONLY_TOOLS 里 → 子 agent 不能再派子 agent。
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in READONLY_TOOLS]


def t_dispatch_subagent(args: dict, ctx: ToolContext) -> str:
    """跑一个只读子 agent 做聚焦调研，返回结论摘要（自带独立上下文，不污染主线）。

    角色决定它拿到什么 system prompt、能用哪些工具、能跑几步（见 subagents.py）。
    **所有角色都是只读的**：子 agent 跑在后台，写/执行类工具的审批没有人能应答。
    """
    task = (args.get("task") or "").strip()
    if not task:
        return "task 为空：描述要子 agent 查清的问题。"
    provider = getattr(ctx, "provider", None)
    if provider is None:
        return "当前环境无可用主脑 provider，无法派子 agent。"
    from . import agent_loop, config, subagents  # 延迟导入避免循环依赖
    role = subagents.get_role(str(args.get("role") or ""))
    try:
        _cap = int(config.get_setting("subagent_max_steps_cap", 40))
    except (TypeError, ValueError):
        _cap = 40
    max_steps = min(int(args.get("max_steps") or role.max_steps), max(1, _cap))
    # 子 agent 有**自己的**预算，不吃主线额度：派三个去查三件事，
    # 不该让主线只剩三分之一的配额。
    sub_ctx = ToolContext(workspace=getattr(ctx, "workspace", ""), plan_mode=True,
                          provider=provider, perm=permission.PermissionState(),
                          progress_reporting_disabled=True)
    sub_messages = [{"role": "system", "content": role.system},
                    {"role": "user", "content": task}]
    try:
        result = agent_loop.run_turn(
            provider, sub_ctx, sub_messages, max_steps=max_steps,
            narrate=lambda s: None,
            tools=subagents.tools_for(role, _subagent_schemas()))
    except Exception as e:  # noqa: BLE001
        return f"子 agent 执行出错：{e}"
    head = "【子 agent 结论】" if role.name == subagents.DEFAULT_ROLE else f"【子 agent · {role.name}】"
    return head + "\n" + (result or "（无结论）")


_DISPATCH["dispatch_subagent"] = t_dispatch_subagent
