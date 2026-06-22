# IvyeaAgent 产品化与 IvyeaOps 嵌入路线图

## 产品定位

IvyeaAgent 后续同时承担两个角色：

- 独立产品：用户可以只安装 IvyeaAgent，在终端里完成亚马逊运营巡检、知识检索、对话 Agent、写入审批和代码/文件任务。
- IvyeaOps 内嵌智能底座：IvyeaOps 通过本地 API 调用 IvyeaAgent，不再要求用户额外部署 Hermes、GBrain、Ollama 等外部项目。

目标不是“包装外部工具”，而是把 Agent、知识库、本地检索做成 Ivyea 自己可交付、可诊断、可升级、可离线降级的产品能力。

## 替代关系

| 现有外部依赖 | IvyeaAgent 内置替代能力 | 产品要求 |
| --- | --- | --- |
| Hermes | Agent Core：对话、多轮任务、工具调用、审批、日志、恢复 | 任务可跑完、可审计、失败可恢复 |
| GBrain | Knowledge Engine：内置/用户知识、来源治理、冲突检测、引用输出 | 来源可追溯、版本可更新、知识可导入 |
| Ollama 检索依赖 | Local Retrieval Engine：全文检索、后续本地 embedding/向量索引、混合召回 | 默认本地可用，缺模型时降级，不影响主流程 |

## 分层架构

1. Agent Core
   - 负责对话循环、任务状态机、工具调用预算、上下文压缩、写操作审批。
   - CLI、IvyeaOps、本地 API 都调用同一个核心能力，避免逻辑散落在界面层。

2. Model Gateway
   - 统一模型 provider、OAuth/API key、模型列表刷新、探活、fallback。
   - 不允许“假装接通”；不可用要返回可诊断错误。

3. Knowledge Engine
   - 管理内置亚马逊知识、用户知识、来源、license、body hash、更新时间、冲突检测。
   - 输出必须能带来源和可信度，区分官方、社区经验、用户自有打法。

4. Local Retrieval Engine
   - 第一阶段提供本地 FTS/LIKE 混合检索，统一召回知识库和记忆。
   - 第二阶段接入本地 embedding 缓存和向量索引。
   - 第三阶段可选接入本地模型推理，但不能把本地模型作为基础功能的硬依赖。

5. Embed/API Layer
   - 提供 `ivyea serve` 本地 HTTP API，给 IvyeaOps 调用。
   - API 只返回 JSON，不暴露密钥，不依赖浏览器交互。

## 阶段计划

### 阶段 1：可嵌入底座

- 新增 `ivyea serve` 本地 API。
- 新增统一 `retrieval` 模块，先整合知识库和记忆检索。
- 给 IvyeaOps 提供稳定端点：健康检查、能力列表、模型状态、知识检索、统一检索。
- 保持 CLI 独立可用。

### 阶段 2：知识库产品化

- 扩展内置亚马逊知识包：广告报表、Listing、Review/Q&A、库存利润、竞品类目、账号节奏。
- 强化来源治理：source manifest、license、hash、retrieved_at、source_url、可信度。
- 用户知识导入支持目录、网页、Markdown、TXT、CSV 摘要。
- 检索结果统一返回引用来源，Agent 回复重要事实时必须带来源。

### 阶段 3：本地语义检索

- 先提供不依赖外部模型的 `local_sparse_vector` 和持久化 `local_hash_embedding_v1` 索引，作为无 Ollama/无 embedding 模型时的降级能力。
- 引入本地 embedding 模型管理：`retrieval embeddings` 可配置 `sentence-transformers`，支持自动下载开关、model path、离线包预置依赖。
- SQLite FTS + 向量索引混合召回。
- 支持索引健康检查、重建、版本迁移；增量索引后续接在当前 SQLite chunk 表之上。
- 无 embedding 模型时自动降级到 FTS/LIKE，并明确告知。

### 阶段 4：Agent Core 产品级闭环

- 先把现有 `task_runner` 暴露为本地 API：任务列表、详情、创建、启动、步骤更新、状态更新、日志追加。
- 多轮任务循环：计划 -> 执行 -> 测试/验证 -> 修复 -> 总结。
- 工具调用预算和中断恢复，避免“工具满了任务跑不完”。
- 对话压缩摘要写入长期记忆，恢复时可追溯。
- IvyeaOps 可以显示任务步骤、日志、工具调用、审批记录。

### 阶段 5：部署和运维

- 提供 `/v1/manifest` 集成发现端点，IvyeaOps 可读取 API 版本、端点、能力和安全边界。
- Windows/macOS/Linux 一键安装和离线包。
- `ivyea self doctor/clean-cache/repair` 覆盖常见环境问题。
- IvyeaOps 启动时自动检测本地 agent 服务，不存在则引导安装或启动。
- 发版流程强制同步 README、门户、版本号、离线包。

## 当前第一轮落地点

本轮先交付：

- `ivyea_agent.retrieval`：统一本地检索入口。
- `ivyea_agent.retrieval_index`：持久化本地 chunk 索引，默认不依赖外部模型。
- `ivyea_agent.retrieval_embeddings`：检索向量后端选择，默认 hash，可选本地 dense embedding。
- `ivyea serve`：本地 HTTP JSON API。
- API 测试和检索测试。

后续所有语义检索、知识库增强、IvyeaOps 集成都挂在这层接口上，避免后面改 IvyeaOps 调用协议。
