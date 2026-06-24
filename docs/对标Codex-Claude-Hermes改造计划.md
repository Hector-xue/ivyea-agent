# Ivyea Agent 对标 Codex / Claude / Hermes 改造计划

目标：把 Ivyea Agent 从“亚马逊广告 CLI 工具”升级为“可扩展的亚马逊运营 Agent 平台”。

## P0 已完成：基础可信度

- 规则引擎、领星读写审批、动作队列、审计回滚、影子模式。
- 多模型 provider、流式输出、计划模式、上下文压缩、resume。
- 本地记忆、AGENTS.md 持久指令、账户画像、Scorecard。
- 通用工具：文件、web、Python、shell，写/执行需人工审批。

## P1 已完成：Skill 系统第一版

- 新增 `ivyea skill list/search/show/run`。
- 支持内置 skill 和用户目录 `~/.ivyea/skills`。
- 对话模式自动按用户问题注入相关 skill。
- 新增 `skill_search` 工具。
- 内置 Amazon skills：
  - `amazon.search_term_optimizer`
  - `amazon.negative_keyword_guard`
  - `amazon.budget_pacing`
  - `amazon.listing_conversion_audit`
  - `amazon.launch_playbook`
  - `amazon.weekly_account_review`

## P2 已完成：知识库可审计第一版

- 知识卡默认带 `source_type`、`confidence`、`retrieved_at`。
- 新增 `ivyea knowledge audit`。
- 搜索结果显示可信度和更新时间。
- 后续扩展时官方文档、社区经验、用户打法必须分层。

## P3 已完成：运行时间线和质量回归

- 新增 `traces.db` 本地运行时间线。
- 工具调用记录 session、turn、工具名、耗时、结果摘要。
- 新增 `ivyea trace recent/stats`。
- Scorecard 汇总工具调用、失败数、平均耗时。
- 新增 `ivyea eval`，覆盖规则引擎 golden、知识召回、skill 召回、安全脱敏。

## P4 已完成：画像和安全增强第一版

- 画像新增毛利率、盈亏 ACOS、价格、币种、Listing 风险、复核日期。
- `profile set` 支持维护这些字段，聊天自动注入。
- 工具展示和 trace 自动脱敏 API key/token/secret/password。
- `run_command` 拦截明显危险命令，如 `git reset --hard`、`rm -rf /`、`mkfs`、`shutdown`。

## P5 已完成第一版：知识库工程化

- `ivyea knowledge import/url/rebuild`。
- `sources.jsonl` 记录 source URL、抓取时间、可信度、来源类型。
- 官方文档优先，社区经验必须标注“经验/非官方”。
- 用户知识卡存 `~/.ivyea/knowledge/`，与内置知识统一检索。
- `doctor` 显示内置/用户知识卡数量。
- `body_hash/license` 元数据已记录。
- `ivyea knowledge index` 构建 SQLite FTS5/LIKE 索引。
- `ivyea knowledge conflicts` 做基础冲突风险审计。
- `ivyea knowledge watchlist` 提供 Amazon 官方/社区来源观察清单。
- `ivyea knowledge plan/apply` 先生成 hash + unified diff，确认后才写入用户知识库并重建索引。

## P5 后续

- 记录摘要版本和来源正文快照。
- 支持更细的冲突提示：官方规则和社区打法不一致时进入知识审核队列。
- 继续扩大官方知识包覆盖面，并补目录/批量导入。

## P6 已完成第一版：Amazon 运营闭环

- Listing 文本/A+/Review/Offer 转化诊断：`ivyea listing audit` 和 `run_listing_audit` 工具。
- Review/Q&A/Offer 归因：`ivyea review audit` 和 `run_review_audit` 工具。
- 库存/利润/价格/Coupon 与广告放量联动：`ivyea offer audit` 和 `run_offer_audit` 工具。
- 竞品/类目关键词诊断：`ivyea competitor audit` 和 `run_competitor_audit` 工具。
- 周期运营复盘：`ivyea weekly review` 聚合动作队列、巡检记忆、Scorecard、trace、影子台账。
- 自动预警：`ivyea alert check` 检查队列、工具失败、影子台账、画像、知识库、磁盘空间。
- 本地计划任务：`ivyea schedule set/list/run-due/run`，由 cron/systemd/IvyeaOps 定时触发。
- 图片资产诊断：`ivyea image audit` 做本地尺寸/比例/命名/缺图预检查，并导出多模态大模型审核 prompt。
- OCR：`ivyea image ocr` 可选调用本机 tesseract，识别图片文字并脱敏。
- 多模态请求包：`ivyea image vision --provider openai|anthropic|gemini` 生成 dry-run/export payload。
- 安全策略：`ivyea policy init/show/check-path/check-command` 管理文件范围和命令 allow/deny。
- 业务 eval 已加入 Listing 意图缺口和 Review 风险回归。

## P6 后续

- 接入多模态视觉模型真实 API 调用。
- 接入真实定时巡检数据源和通知渠道。
- 更细的审批策略：按工具/路径/命令分级设置“只读/询问/拒绝/本会话允许”。
- 新品 0-30-60 天启动计划自动化。
- 周报/月报、异常预警、定时巡检。

## P7 下一批：更强 Agent 执行架构

- 子 agent：数据分析、Listing 审核、广告执行、知识审核分工。
- 长任务可恢复：任务状态持久化、失败重试、继续执行。
- 工具权限策略文件：allow/deny/path scope/high risk policy。
- 更完整的 run timeline：模型输出摘要、知识/skill 命中、审批结果。

## P8 下一批：业务 Eval 深化

- 多份历史报表 golden set。
- 否词误伤率评估。
- 预算/调 bid 建议准确率。
- 知识召回质量评估。
- 执行后 7/14/30 天效果复盘。
- 每次 release 前跑 `ivyea eval` + pytest + ruff。

## P9 最后：MCP 深化

- MCP 工具 schema 自动探测和写入映射向导。
- dataSource/writeActions 校验真实工具参数。
- 写入 dry-run 模拟、回滚预检查。
- 与 IvyeaOps 反向集成。
