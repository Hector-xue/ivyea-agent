# IvyeaAgent Amazon 专业知识库推进方案

## 目标

把 Amazon 问答从“临时网页搜索 + 模型概括”升级为“分级证据库 + 按风险检索 + 可验证引用 + 持续更新”。专业性不以知识卡数量衡量，而以来源权威、适用范围、时效、可追溯和回答约束衡量。

第一阶段已经实现底座和首批高风险知识；这不等于 Amazon 全量知识已经完成入库。

## 复核后的关键决策

1. **不做无差别网页抓取。** 公开网页、登录后帮助文档、动态 API schema、账户通知和社区经验使用不同入口与证据等级。
2. **官方变化不自动发布。** 监控器只保存快照、hash、diff 和待审事件；审核确认后才生成/更新知识卡，避免页面噪声或误解析污染回答。
3. **Seller Central 不绕过认证。** 登录后帮助、绩效通知、case 回复只能通过用户授权的导出/API 导入，并标记站点、账户与时间。
4. **动态要求优先查动态 schema。** 类目属性、枚举和上架要求优先使用目标 marketplace/product type 的 Product Type Definitions，而不是静态摘要。
5. **算法说法必须分级。** 公开文档支持的内容是官方事实；账户报表得出的关系是数据推断；“权重/流量池/惩罚”等未公开机制是运营假设。
6. **引用绑定结论而不是装饰答案。** 模型用 `[K#]` 标记实际证据，系统校验编号并只输出实际引用来源；不允许检索 A、回答 B、末尾机械挂 A。

## 知识域

| 域 | 首要官方来源 | 更新方式 | 风险 |
|---|---|---|---|
| 卖家注册与身份验证 | Sell on Amazon 注册指南、授权 Seller Central Help | 公开监控 + 授权导入 | 高 |
| 上架与错误码 | Listings Items troubleshooting、Product Type Definitions | 文档监控 + 授权 API | 高 |
| 店铺绩效与申诉 | Seller Central Help、账户通知、case 回复 | 授权导入 | 高 |
| 政策与合规 | Seller Central Help、政策页 | 授权导入 + 公开锚点 | 高 |
| 费用、税务与结算 | 官方费率/帮助页、账户报表 | 按站点版本化 | 高 |
| FBA、库存与履约 | FBA 官方文档、Seller University | 公开监控 + 授权数据 | 中高 |
| Listing 与转化 | Listing 指南、A+、实验工具 | 公开监控 | 中 |
| Amazon Ads | Ads Help/API/指南/发布说明 | 公开监控 + API | 中高 |
| 搜索流量与排名 | 官方公开说明 + 账户数据 | 事实/推断/假设分层 | 中高 |
| 品牌、知识产权与受限商品 | 官方政策/Brand Registry | 授权导入 | 高 |
| 新闻与功能更新 | Selling Partner Blog、Ads Newsroom、SP-API RSS | 6–24 小时监控 | 中 |
| 账户本地案例 | 通知、错误 payload、case、操作结果 | 用户授权导入 | 账户级 |

## 数据与发布流程

```text
官方来源注册表
  → 公开监控 / 授权导入 / 授权 API
  → 原始快照 + URL + 时间 + hash
  → 变化检测与 diff
  → 人工/规则审核
  → 结构化知识卡（authority/evidence/marketplace/locale/freshness）
  → 本地索引
  → 风险路由检索
  → [K#] 引用校验与来源清单
```

发布审核至少检查：来源确属 Amazon、内容没有被登录页/导航污染、适用站点和类目明确、摘要没有扩大原文含义、旧知识是否冲突、是否需要迁移/失效日期。

## 查询路由

- 高风险：注册、验证、报错、绩效、停用、申诉、政策、合规、费用、知识产权。必须检索；无证据时明确缺口。
- 中风险：广告、Listing、FBA、流量、排名、算法。优先检索；说明账户数据和假设边界。
- 低风险或非 Amazon：不强行注入 Amazon 知识，避免污染工程/通用任务。
- 诊断型问题应补问 exact code/message、marketplace、category/product type、SKU/ASIN、时间和数据来源。

## 更新与运行

- SP-API changelog RSS：建议每 6 小时。
- SP-API release/deprecation/metadata：建议每 12–24 小时。
- Selling Partner Blog、Amazon Ads Newsroom：建议每 12 小时。
- 稳定指南：建议每 72–168 小时。
- 登录后 Seller Central 内容：按授权导出周期或重大通知触发，不自动爬取。

推荐计划任务：

```bash
ivyea schedule set amazon-updates knowledge_sync --every-hours 6
ivyea schedule run-due
ivyea knowledge changes
```

## 验收指标

- 高风险基准问题 Top-1 命中正确官方卡。
- 官方事实引用有效率 100%，不存在未知 `[K#]`。
- 内置知识卡来源 URL 完整率 100%。
- 每条变更有旧/新快照、hash、时间和来源，可回溯。
- 站点/类目不明时不输出伪确定结论。
- 社区/旧 GBrain 内容不得压过当前官方证据，不得标为官方事实。
- 全量自动测试、静态检查和真实官方源响应解析通过。

## 分阶段推进

### 阶段 1：可靠性底座（已完成）

- 官方来源注册表、授权边界、增量监控与审核队列。
- authority/evidence/marketplace/locale 元数据。
- 风险检索路由、`[K#]` 引用门禁、确定性来源清单。
- 注册验证、上架错误、SP-API 授权错误、证据标准首批卡。
- CLI、HTTP API、计划任务和产品 eval。

### 阶段 2：高风险域扩充（已完成第一版）

- 已按 marketplace 建立 US、UK/EU、JP 注册，以及绩效/申诉、费用、受限商品、危险品、IP、变体和 GTIN 专题。
- 已引入授权 Seller Central 证据 JSON Schema、专项脱敏、diff 确认和私有证据台账；不保存原始证件。
- 已建立 error code/message、marketplace、ASIN/SKU、product type、status/policy、缺失输入和诊断就绪状态等结构化字段。

### 阶段 3：广告与流量专业化（已完成第一版）

- 已将 Ads Help/API/release notes、指标、归因、搜索词/Targeting/Placement 报告和竞价叠加规则纳入官方监控。
- 已加入带日期的投放产品能力矩阵；API report type/字段/版本等动态能力要求在开发者门户或账户现场核验，不硬编码为永久事实。
- 已实现 `advertising_report` 与 `traffic_experiment` 结构化分析，记录产品、报表、窗口、时区、币种、归因、销售范围、变更因素、对照和混杂因素。
- 已实现 CTR/CPC/CVR/ACoS/ROAS 确定性计算和零分母保护；归因销售不会被当作增量销售。
- 已实现官方事实、账户观测、账户推断、运营假设分层检索，强制阻断账户现象升级为官方算法结论。

### 阶段 4：规模化运营（已完成第一版）

- 已建立稳定变更事件 ID、pending/approved/rejected/superseded 状态、显式确认、快照完整性校验和不可变审核历史。
- approved 只允许继续生成导入草案，不会自动发布；知识发布仍必须独立生成 diff 并确认应用。
- 已建立知识治理总看板、关键知识域 × marketplace 覆盖矩阵、知识卡/官方来源时效看板和自动预警。
- 已增强冲突检测，识别用户/旧知识与官方主题重叠的反向表述、未公开算法和绝对数值规则，以及伪 official 来源。
- 已将持续评测数据化，首版覆盖注册、上架错误、绩效、费用、合规、Listing、广告与流量算法边界 15 个基准问题，可通过 CLI/API/计划任务运行。
- 当前覆盖矩阵主动保留未覆盖站点和高风险域，不使用 GLOBAL 或通用 API 卡虚增注册、绩效、费用、受限商品和危险品的本地政策覆盖。

### 阶段 5：可视化治理与审核闭环（已完成第一版）

- IvyeaOps `/brain?tab=governance` 已接入治理总览、变更审核、覆盖矩阵、时效监控、质量评测和冲突风险页面。
- 已实现 approved 变更的审核包：校验快照 hash，展示官方 diff/快照摘录，并按 URL、category 和 topics 推荐受影响知识卡。
- 审核人必须编辑精简后的知识正文并预览 diff；二次确认后发布独立运行时官方更新卡，不直接改写安装包中的内置卡。
- 发布卡保留 change-event、source-hash 和 review-target，并写入独立 publication 台账；同一事件默认禁止重复发布。
- IvyeaOps 浏览器只访问同源 FastAPI 代理，FastAPI 再访问本机 IvyeaAgent；审核、同步、草案和发布接口要求管理员权限。
