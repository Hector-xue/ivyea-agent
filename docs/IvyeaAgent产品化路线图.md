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
   - 提供来源观察清单、更新草案 diff、确认写入和索引同步，避免论坛经验未经审核污染知识库。
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
- 给 IvyeaOps 提供稳定端点：健康检查、能力列表、模型状态、只读 Agent 对话、知识检索、统一检索。
- 保持 CLI 独立可用。

### 阶段 2：知识库产品化

- 扩展内置亚马逊知识包：广告报表、Listing、Review/Q&A、库存利润、竞品类目、账号节奏。
- 强化来源治理：source manifest、license、hash、retrieved_at、source_url、可信度。
- 新增 Amazon 来源观察清单，官方来源可作为高置信摘要入口，社区来源默认 review_required。
- 新增知识更新草案：先生成 hash 和 unified diff，确认后再写入用户知识库并重建索引。
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
- 提供 `/v1/chat` 只读嵌入式 Agent 入口，默认计划模式，返回工具事件和脱敏消息；写操作仍保持禁用。
- 多轮任务循环：计划 -> 执行 -> 测试/验证 -> 修复 -> 总结。
- 工具调用预算和中断恢复：接近上限提前提示，达到上限时写入会话续跑上下文；绑定 task 时记录结构化 `resume`，包含暂停原因、下一步、工具调用数量和可注入下一轮的续跑 prompt；`task continue` / `/v1/tasks/{id}/continue` 可直接从续跑点跑一轮。
- 对话压缩摘要写入长期记忆，恢复时可追溯。
- IvyeaOps 可以显示任务步骤、日志、工具调用、审批记录，并通过 `/v1/tasks/{id}/resume` 读取续跑点。

### 阶段 5：部署和运维

- 提供 `/v1/manifest` 集成发现端点，IvyeaOps 可读取 API 版本、端点、能力和安全边界。
- 远程监听必须配置 Bearer token；localhost 默认免 token，保证开发/嵌入简单。
- Windows/macOS/Linux 一键安装和离线包。
- `ivyea self doctor/clean-cache/repair` 覆盖常见环境问题。
- IvyeaOps 启动时自动检测本地 agent 服务，不存在则引导安装或启动。
- 发版流程强制同步 README、门户、版本号、离线包。

## 当前已落地点

本轮已经交付：

- `ivyea_agent.retrieval`：统一本地检索入口。
- `ivyea_agent.retrieval_index`：持久化本地 chunk 索引，默认不依赖外部模型。
- `ivyea_agent.retrieval_embeddings`：检索向量后端选择，默认 hash，可选本地 dense embedding；离线包可预置 `sentence-transformers` 依赖和模型目录。
- `ivyea_agent.knowledge`：来源登记、审计、冲突检测、来源观察清单、更新草案 diff、确认写入和索引同步。
- `ivyea_agent.self_manage`：本地服务 status/start/stop/logs/autostart 模板，给 IvyeaOps 安装页和设置页调用。
- `ivyea_agent.models`：provider 能力矩阵、实时模型清单、探活诊断，避免“假装切换成功”。
- `ivyea_agent.code_agent`：代码任务 plan/context/bundle、受控 apply/test/repair loop 和审计记录。
- `ivyea serve`：本地 HTTP JSON API，manifest/openapi 自动暴露上述能力。
- IvyeaOps 代理层：bootstrap、manifest、service 管理、模型目录/probe、retrieval、code bundle/apply-loop、knowledge watchlist/draft/apply。
- API、检索、知识库、离线包、代码闭环和 IvyeaOps 代理测试。

后续所有语义检索、知识库增强、IvyeaOps 集成都挂在这层接口上，避免后面改 IvyeaOps 调用协议。
