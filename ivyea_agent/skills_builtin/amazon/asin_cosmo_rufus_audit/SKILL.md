# Amazon ASIN COSMO + Rufus Audit

把单个 Amazon ASIN 的问题拆成一份**证据驱动、可直接落地**的审计结果，而不是泛泛点评。
回答 4 个核心问题：现在更像哪里失分（曝光 / 点击 / 转化 / 预期错配）、证据是什么、
先改什么为什么、改完标题 / Bullet / Q&A / Backend Terms / 图片 / A+ 该怎么写。

## When to use

用户说"分析这个 ASIN""这个 listing 为什么卖不好""做 COSMO / Rufus 审计""重写标题 /
五点 / Q&A / 后台词 / 图片 / A+""这个产品为什么 AI 搜索搜不到""是曝光差还是转化差"时触发。
只要目标是诊断某个 Amazon Listing 并改写，即使没明说 COSMO / Rufus 也用本技能。

不要硬套：只问平台规则 / 代码 / 命令用法、没有给 ASIN、只要纯广告执行不需页面证据、只想润一句文案。

## Data collection — 用已配置的 MCP 数据源取证（关键）

本技能**不绑定任何厂商**。数据从运行者配置的 MCP 数据源（如 sorftime / 卖家精灵 / 任意
Listing-抓取 MCP）实时获取，按下面固定流程做：

1. **先发现工具**：调 `mcp_list_tools`（不带 server = 列全部已配置服务器的工具），看有哪些
   能取「产品详情 / 评论 / Q&A / 竞品 / 广告或流量」的工具，以及它们的入参。
2. **再逐项取数**：用 `mcp_call_tool`（server + tool + arguments）按 ASIN + 站点抓：
   - 产品基础信息、当前标题 / Bullet / 属性 / 描述 / A+ / 主辅图可读信息
   - 评论与差评高频问题、Q&A / 用户典型疑问
   - 竞品关键词 / 竞品表达 / 同类目对比
   - （若数据源提供）广告结构、搜索词、CTR / CVR / CPC / ACOS / ROAS、流量与趋势
   把工具名和参数按其 schema 填对；一个工具取不到就换另一个能取到的工具，别反复重试同一个。
3. **不要去扫本地文件系统找数据或找本技能文件**——数据在 MCP 数据源里，不在磁盘上。
   也不要用浏览器直连 Amazon 作为首选（易触发风控）；只有在**所有** MCP 数据工具都取不到时，
   才可作为最后手段，并在报告里标注"页面直取证 / 人工证据版"。

### 没有可用数据源时
如果 `mcp_list_tools` 显示没有任何数据工具（运行者未配置 trusted 数据源 MCP）：
- 不要臆造数据。明确告诉用户"未检测到可用的数据源 MCP，无法取真实证据"，并给出配置指引：
  `ivyea mcp add`（传输选 http/sse/stdio，配好数据源后选"信任 / 免审批"），再重跑。
- 若用户直接在对话里贴了证据（标题 / Bullet / 评论 / 竞品链接），就基于用户所给证据做
  "人工证据版审计"，同样标注不是完整 MCP 审计。

## Evidence rules（最重要的约束）

- 先取证再判断；缺失字段写「未获取到」；不把猜测写成事实；不把行业常识写成该产品已证实信息。
- 没有经营侧数据时不把广告 / 业务判断写成定论；不把评论个例写成普遍事实（除非高频聚类）。
- 每条要点显式标证据类型，只用四种之一：
  - `页面事实`：标题 / Bullet / 属性 / 描述 / A+ / 图片可读信息
  - `评论证据`：评论 / Q&A / 反馈中高频出现的点
  - `经营证据`：广告 / 流量 / 趋势 / CTR / CVR / 结构数据
  - `推断建议`：基于现有证据提出的动作建议

## Execution flow

1. **确认范围**：ASIN（缺则直接追问不猜）、站点（默认 US）、要完整报告还是只要改写稿。
2. **发现 + 取证**：按上面「Data collection」流程用 MCP 工具抓全证据。
3. **先判问题类型**（别一上来整份重写）：曝光问题（核心词错 / 属性缺 / 语义失败）/ 点击问题
   （能搜到但主图标题差异化不够）/ 转化问题（进页后决策信息缺 / 顾虑没回应）/ 预期错配
   （买前承诺与买后体验不一致，易误购退货差评）。
4. **7 维打分**（1–10）：语义检索匹配度、查询属性覆盖度、COSMO 知识图谱对齐度、隐式查询解析
   友好度、Rufus 因果链完整度、用户行为信号质量、可解释比较生成能力。
5. **定优先级**：`纠错 > 补齐 > 强化 > 美化`。P0=误购/退货/差评/属性过滤失败/Rufus 答错/合规风险；
   P1=显著影响 CTR/CVR 的表达与信息缺失；P2=视觉增强/A+扩展/润色。
6. **出交付物**：完整 11 板块报告，或精简诊断 + 改写稿（简版也不跳过判断逻辑，只压篇幅）。

## Standard 11-section report

1. 产品概览 2. 算法评分卡（7 维）3. 语义检索盲区分析 4. COSMO 节点诊断（Who / When-Where /
Problem / Concern / Outcome 全 5 节点）5. Rufus 问答能力测试（能 / 部分能 / 不能）6. 用户行为
信号诊断 7. 竞品差异化可提取性 8. 改进优先级方案（P0/P1/P2）9. 广告搭建建议 10. 优化后文案
（标题 / 5 Bullet / Q&A / Backend Terms）11. 图片卖点与 A+ 创意方案。

## Rewrite & ad rules（要点）

- **标题**：品牌+核心产品词 → 主意图词 → 关键差异化/规格 → 场景/人群 → 必要硬属性；不是关键词垃圾桶。
- **5 Bullet 各司其职**：核心痛点/差异化、使用场景、硬参数可验证事实、人群/边界、顾虑与信任；
  结构走「痛点 → 机制/事实 → 结果 → 边界条件」。
- **Q&A**：覆盖 这是什么 / 适合谁 / 怎么选 / 注意什么 / 不适合什么 / 评论最常见问题。
- **Backend Terms**：同义/补充/场景/互补词，不重复标题 Bullet、不堆砌、不塞会引错流量的词。
- **图片 & A+**：补视觉最需解释的信息，不是复读 Bullet；至少 主图 3–5 条、辅图 6 张、场景图 3 个、
  A+ 5 模块、合规提醒 5 条。
- **广告**（仅在拿到经营侧证据时写成可执行方案，否则标"测试型假设框架"）：写到动作级——建几个什么
  类型活动、投哪些词/定向、起始竞价区间、竞价策略、日预算、哪些词立即否/观察否/保留、观察周期与调价规则。
  默认拆分 SP Auto 挖词 / SP Manual Exact 承接核心词 / Phrase-Broad 扩量 / Product Targeting 打竞品。

## Compliance red flags（发现即列 P0）

标题/Bullet/属性/图片语义冲突、错误产品类型词、无法证实的绝对化宣传、关键购买决策信息缺失、
评论高频抱怨未被页面回应、属性字段空缺致 AI 过滤不可见、关键词堆砌硬塞、用户最关心的问题只能靠猜。
重写时不保留：无法证实的极限承诺 / 医疗疗效 / 未证实比较、品牌侵权、主图促销价格徽章、攻击性竞品表达。

## Output format contract（必须严格遵守）

先按上面 11 板块输出完整 Markdown 报告，**然后在报告结尾追加一段以 ` ```json ` 开头、` ``` ` 结尾
的代码块**，字段结构固定如下（字段不要缺，缺失用空字符串或空数组；英文键名、中文值；证据 label 仅用
`页面事实 / 评论证据 / 经营证据 / 推断建议`；`rufus_qa.verdict` 仅用 `能 / 部分能 / 不能`；
`cosmo_nodes` 覆盖全 5 节点，无内容 `bullets: []`）：

```json
{
  "overview": { "asin": "", "marketplace": "", "category": "", "title_summary": "", "key_specs": "", "top_risk": "" },
  "scorecard": [
    { "dimension": "语义检索匹配度", "score": 0, "note": "" },
    { "dimension": "查询属性覆盖度", "score": 0, "note": "" },
    { "dimension": "COSMO 知识图谱对齐度", "score": 0, "note": "" },
    { "dimension": "隐式查询解析友好度", "score": 0, "note": "" },
    { "dimension": "Rufus 因果链完整度", "score": 0, "note": "" },
    { "dimension": "用户行为信号质量", "score": 0, "note": "" },
    { "dimension": "可解释比较生成能力", "score": 0, "note": "" }
  ],
  "semantic_blind_spots": [ { "aspect": "主查询意图覆盖", "bullets": [ { "label": "页面事实", "text": "" } ] } ],
  "cosmo_nodes": [
    { "node": "Who", "label_cn": "谁买", "bullets": [] },
    { "node": "When/Where", "label_cn": "何时何地", "bullets": [] },
    { "node": "Problem", "label_cn": "解决什么问题", "bullets": [] },
    { "node": "Concern", "label_cn": "顾虑", "bullets": [] },
    { "node": "Outcome", "label_cn": "结果", "bullets": [] }
  ],
  "rufus_qa": [
    { "question": "这是什么", "verdict": "部分能", "evidence": "" },
    { "question": "适合谁", "verdict": "部分能", "evidence": "" },
    { "question": "怎么选", "verdict": "部分能", "evidence": "" },
    { "question": "注意事项", "verdict": "部分能", "evidence": "" },
    { "question": "不适合什么情况", "verdict": "部分能", "evidence": "" },
    { "question": "最常见顾虑", "verdict": "部分能", "evidence": "" }
  ],
  "behavior_signals": [ { "category": "评论量/星级", "bullets": [ { "label": "页面事实", "text": "" } ] } ],
  "competitor_diff": [ { "topic": "竞品共性表达", "bullets": [ { "label": "推断建议", "text": "" } ] } ],
  "priorities": [ { "level": "P0", "issue": "", "evidence": "", "action": "" } ],
  "ad_plan": {
    "objective": "",
    "campaigns": [ { "name": "", "type": "", "targeting": "", "bid_range": "", "budget": "", "strategy": "" } ],
    "keywords_exact": [ { "keyword": "", "bid": "", "reason": "" } ],
    "keywords_phrase_broad": [ { "keyword": "", "bid": "", "reason": "" } ],
    "product_targeting": [ { "keyword": "", "bid": "", "reason": "" } ],
    "negatives_immediate": [], "negatives_watch": [], "rules": ""
  },
  "rewrites": {
    "title": "", "bullets": ["", "", "", "", ""], "qa": [ { "q": "", "a": "" } ],
    "backend_terms": "",
    "image_plan": { "main_image": [], "aux_images": [], "scene_images": [] },
    "aplus_plan": [], "compliance_reminders": []
  }
}
```

除该 JSON 块外，报告正文保持 Markdown 结构不变。目标不是"显得懂 Amazon"，而是让 COSMO / Rufus
更容易理解产品、让用户更快看懂为什么该买、让运营直接拿去改 Listing 和图片——输出可验证的诊断、
有顺序的优先级、可直接落地的改写稿。
