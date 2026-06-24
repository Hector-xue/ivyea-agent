# Ivyea Agent

自托管的**亚马逊运营 CLI Agent**。先做独立命令行工具，成熟后嵌入 IvyeaOps 控制台。

> 哲学：**确定性规则引擎 + LLM 复核**；证据驱动+标签化；写操作审核制；护栏内置；数据私有。

## 快速入口

- 门户网站：`https://agent.ivyea.com`（静态站点源码在 `site/`）
- 完整部署指南：[docs/部署指南.md](docs/部署指南.md)
- 操作文档：[docs/使用与操作文档.md](docs/使用与操作文档.md)
- 最新 Release：`v1.0.19`（main 分支可能包含尚未打包的新改动）

## 三分钟安装

Linux / macOS：

```bash
curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash
ivyea config
ivyea chat
```

Windows PowerShell：

```powershell
iwr https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.ps1 -UseBasicParsing | iex
ivyea config
ivyea chat
```

固定版本安装：

```bash
curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | IVYEA_VERSION=v1.0.19 bash
```

```powershell
$env:IVYEA_VERSION="v1.0.19"
iwr https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.ps1 -UseBasicParsing | iex
```

一键脚本默认安装 GitHub 最新 Release wheel；Release 不可用时自动回退到 git 源码安装。私有仓库读取 Release 可设置 `GITHUB_TOKEN`。安装完成后脚本会尽量运行 `ivyea self doctor`，检查 Python、PATH、数据目录和可选依赖。
如果用户机器缺少基础环境，脚本会尽量自动补齐：Linux 用 `apt/dnf/yum/apk`，macOS 用 Homebrew，Windows 用 winget；无法自动安装时会给出明确提示。团队内网或离线环境建议使用下文的离线 bundle。

## IvyeaOps 嵌入模式

IvyeaAgent 可以独立作为 CLI 使用，也可以作为 IvyeaOps 的本地智能底座启动：

```bash
ivyea serve --host 127.0.0.1 --port 8765
ivyea self ops-bootstrap
```

`serve` 默认只允许监听 localhost；如果要绑定 `0.0.0.0`，必须显式加 `--allow-remote` 且提供 `--api-token` 或 `IVYEA_API_TOKEN`，避免本地 API 被误暴露。

当前本地 API 提供：

- `GET /health`：健康检查、版本、模型状态、知识库数量、检索能力。
- `GET /v1/manifest`：IvyeaOps 集成发现清单，包含 API 版本、端点、能力和安全边界。
- `GET /v1/openapi.json`：OpenAPI 3.1 接口发现文档，供 IvyeaOps 或其它客户端自动生成调用层。
- `GET /v1/mcp/self-config`：返回本机只读 stdio MCP server 配置，供 IvyeaOps 或其它 MCP 客户端自动接入。
- `GET /v1/capabilities`：本地检索能力说明。
- `GET /v1/model/providers`：模型 provider 能力矩阵，包含认证状态、默认模型、工具调用/流式/视觉/实时模型清单/probe/本地端点等能力标签，不返回密钥。
- `GET /v1/system/status`、`GET /v1/system/doctor`：安装状态和诊断检查，供 IvyeaOps 安装页/设置页展示。
- `GET /v1/system/bootstrap`：IvyeaOps 本地自发现契约，包含启动命令、健康检查 URL、manifest/openapi URL、stdio MCP 配置、安装命令和 systemd/launchd/Windows 自启动模板。
- `POST /v1/chat`：运行一轮 IvyeaOps 嵌入式 Agent 对话；默认只读计划模式，返回回答、工具事件和脱敏消息。
- `POST /v1/chat/stream`：同样的只读 Agent 对话，但用 Server-Sent Events 实时返回 token、工具事件和 final。
- `GET/POST /v1/chat/sessions`：列出/创建本地持久会话。
- `GET /v1/chat/sessions/{id}`：读取会话详情，供 IvyeaOps 续接对话。
- `GET /v1/skills`、`GET /v1/skills/search?q=否词`、`GET /v1/skills/{id}`：浏览和搜索内置/用户 Skills。
- `GET /v1/knowledge/cards`、`GET /v1/knowledge/cards/{id}`：浏览知识卡及详情。
- `POST /v1/knowledge/cards`：创建用户知识卡。IvyeaOps 上传文件时应在前端/后端读取正文后传 `body`，服务端不直接读取任意本机路径。
- `GET /v1/knowledge/audit`、`GET /v1/knowledge/conflicts`：知识来源、时效、许可、冲突风险审计。
- `GET /v1/knowledge/sources`：知识来源登记表，按来源 URL/类型/license 聚合知识卡，标记缺失来源、过期和 review_required。
- `GET /v1/knowledge/watchlist`：内置 Amazon 官方文档、Seller Central、SP-API、Amazon Ads 和社区来源观察清单；社区来源默认 review_required。
- `POST /v1/knowledge/update/draft`：给知识更新生成草案、hash 和 unified diff，不写入本地知识库。
- `POST /v1/knowledge/update/apply`：带 `confirm: true` 后才应用草案，并可自动重建知识索引和统一检索索引。
- `POST /v1/knowledge/rebuild`：校验用户知识元数据，并重建知识索引和统一检索索引。
- `GET /v1/knowledge/search?q=否词&limit=5`：亚马逊知识库检索。
- `GET /v1/retrieval/embeddings`：本地检索向量后端状态，区分默认 hash 和真实 dense embedding 是否可用。
- `GET /v1/retrieval/status`：持久化本地检索索引状态，包含 backend、chunks、更新时间和索引库位置。
- `POST /v1/retrieval/search`：统一检索知识库 + 记忆 + 本地持久索引。
- `POST /v1/retrieval/embeddings`：配置本地检索向量后端。
- `POST /v1/retrieval/embeddings/probe`：真实加载/编码一次，确认 dense embedding 是否可用；失败会报告原因并继续降级到本地 hash 索引。
- `POST /v1/retrieval/index`：重建持久化本地检索索引；传 `{"sync": true}` 时只在知识/记忆/embedding fingerprint 变化后重建，给 IvyeaOps 安装后初始化或知识库更新后调用。
- `GET/POST /v1/tasks`、`GET /v1/tasks/{id}/resume`、`POST /v1/tasks/{id}/continue`：长任务列表、创建、状态推进、日志追加、结构化续跑提示和一轮自动续跑，供 IvyeaOps 展示 Agent 执行过程并从中断点继续。
- `GET /v1/traces`、`GET /v1/traces/stats`：运行时间线和工具调用统计，供 IvyeaOps 展示执行过程。
- `POST /v1/workspace/index/search/inspect/symbols/impact`：只读项目理解能力，供 IvyeaOps 做代码库扫描、符号搜索和影响面分析。
- `POST /v1/code/plan/context/bundle/apply-loop/quality/review/repair`：代码 Agent 工作流，输出任务计划、紧凑上下文、多轮任务包；`apply-loop` 支持受控 patch apply/test/repair 审计，默认 dry-run。

独立 CLI 也可以直接调用统一检索：

```bash
curl -s http://127.0.0.1:8765/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"主图点击高但转化低，应该先看什么？","max_steps":6}'

ivyea retrieval index
ivyea retrieval sync
ivyea retrieval status --json
ivyea retrieval embeddings
ivyea retrieval search "高点击 零单 是否否词"
ivyea retrieval search "预算 品牌词" --json
```

默认索引后端是 `local_hash_embedding_v1`：它不依赖 Ollama/GBrain/外部向量库，会把内置/用户知识卡和长期运营记忆一起写入本地 SQLite 索引，适合作为 IvyeaOps 默认可用的本地召回底座。需要真正的本地 dense embedding 时，可安装 semantic extra 并配置：

```bash
python -m pip install "ivyea-agent[semantic]"
ivyea retrieval embeddings --backend sentence-transformers --model BAAI/bge-small-zh-v1.5 --allow-download
ivyea retrieval embeddings --probe
ivyea retrieval index
```

生产离线环境建议在发版机上预置依赖和模型缓存；没有语义依赖、模型目录不可用或真实加载失败时都会自动降级到 hash 索引，并在 `retrieval embeddings/status` 或 `retrieval embeddings --probe` 中显示原因。`retrieval status` 会显示 `needs_rebuild`，`retrieval sync` 只有在知识卡、长期记忆或 embedding 后端 fingerprint 变化时才重建索引。

## 一键部署包（给用户提前准备好）

如果你希望用户拿到一个压缩包后直接安装，而不是临时去下载 Python 依赖，可以在发版机上提前生成离线包：

```bash
python scripts/build_offline_bundle.py
```

如果要把本地语义检索依赖也放进离线包：

```bash
python scripts/build_offline_bundle.py --with-semantic
```

如果要提前把 embedding 模型也放进离线包，发布机先下载好模型目录，再打包：

```bash
python scripts/build_offline_bundle.py \
  --with-semantic \
  --semantic-model-dir /models/bge-small-zh-v1.5 \
  --semantic-model-name BAAI/bge-small-zh-v1.5
```

产物在 `dist/offline/`：

- `ivyea-agent-offline-版本.zip`：给 Windows 用户。
- `ivyea-agent-offline-版本.tar.gz`：给 Linux / macOS 用户。
- 包内包含 `install.sh`、`install.ps1` 和 `wheelhouse/` 依赖缓存。
- 如果带 `semantic-manifest.json` 和 `models/embedding/`，安装脚本会把模型复制到 `~/.ivyea/models/embedding/`，配置 `sentence-transformers` 本地模型路径，并执行 `ivyea retrieval sync`。

用户解压后执行：

```bash
bash install.sh
```

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

离线包会优先从本地 `wheelhouse/` 安装到 `~/.ivyea/runtime`，并生成 `ivyea` 启动器，不再下载 Python 包。安装脚本会用包内启动器运行 `ivyea self doctor` 做诊断。若机器没有 Python 3.9+，在线安装脚本会尝试自动安装；纯离线场景需要管理员提前装好 Python。

发布机也可以生成单文件可执行入口：

```bash
python -m pip install pyinstaller
python scripts/build_standalone.py --clean
```

产物在 `dist/standalone/`。这个脚本用于发布机，不要求普通用户本机安装 PyInstaller；跨 Windows/macOS/Linux 需要分别在对应系统上构建。

## 只读广告巡检（入门）

输入一份亚马逊搜索词报告（CSV/xlsx），输出可执行的**只读巡检报告**：否词候选 / 放量 / 降 bid / Listing 反馈 / 观察 / 人工复核，每条带证据与置信度。**不会自动改广告。**

规则引擎复用了成熟的搜索词决策逻辑（`zach-search-term-report-analyzer`，已 vendor），LLM 只做复核（词分类/归因/护栏检查/措辞），不推翻数据。

## 通用代码 Agent 能力

除了亚马逊运营专长，Ivyea Agent 也内置代码工程闭环，用来对标 Claude Code / Codex / Hermes 的基础工作流：先理解项目，再规划修改，再用结构化 patch、测试和审查门禁收口。

```bash
ivyea workspace index --root .                     # 建立项目索引，含 Python AST 与 JS/TS 轻量定义/调用
ivyea code plan "给 CLI 增加一个导出命令" --root .  # 任务拆解、相关文件、测试建议
ivyea code context "给 CLI 增加一个导出命令" --root . # 输出紧凑代码上下文
ivyea code bundle "给 CLI 增加一个导出命令" --root . # 多轮任务包：计划/上下文/影响面/测试/续跑提示
ivyea workspace symbols add --root .               # 查符号定义位置
ivyea code impact add --root .                     # 查调用方、导入方、受影响测试
ivyea code refs add --root .                       # 查定义/导入/调用/文本引用
ivyea code rename-plan add --new-name sum_values --root . # 生成重命名 patch 草案
ivyea code brief "给 CLI 增加导出命令" --root . --budget 6000 # 生成预算内上下文
ivyea code quality --root .                        # 本地复杂度/大文件/长函数/覆盖风险
ivyea code diff-brief --root .                     # 生成变更摘要和 PR 草稿
ivyea code release-check --root . --version v0.5.7 # 发布前只读门禁汇总
ivyea code patch "给 CLI 增加导出命令" --root . --llm # 生成 LLM patch 请求包；不调用模型
ivyea code run "给 CLI 增加导出命令" --root . --llm-patch --max-rounds 3
ivyea code apply-loop --root . --patch-spec patch.json --test-command "python -m pytest" # dry-run 结构化应用/测试/修复闭环
ivyea code apply-loop --root . --patch-spec patch.json --test-command "python -m pytest" --execute --yes
ivyea code runs                                    # 查看已保存的 code run
ivyea code sandbox --root .                        # 生成 git worktree/临时目录沙箱计划，不执行
ivyea patch validate patch.json --root .           # dry-run 校验结构化补丁
ivyea patch apply patch.json --root . --execute    # 明确执行后才写入
ivyea code test --root . --command "python -m pytest tests/test_cli.py"
ivyea code repair --root . --output-file pytest.out # 解析失败测试，生成下一轮修复计划
ivyea code review --root .                         # diff 风险、建议测试和发布前门禁
```

代码能力遵循同一条安全原则：规划、上下文、修复计划和审查默认只读；写文件需要 `patch apply --execute`，提交、推送、发版不会被 `ivyea code` 自动触发。

## 对话体验设置

长任务默认尽量跑完整，不再为了省 token 自动压缩上下文。上下文变长时 Ivyea 只提示你可以手动压缩：

```bash
/compact              # 手动把当前会话历史压成摘要
/compact auto status  # 查看自动压缩状态和阈值
/compact auto on      # 明确开启自动压缩
/compact auto off     # 关闭自动压缩（默认）
```

工具调用单轮上限默认 48 步；接近上限时会提前提示，达到上限时会把“从最后未完成步骤继续、不要重复已成功工具调用”的续跑指令写回对话上下文。如果一次代码任务、资料整理或广告分析确实需要更多工具往返，可以调高：

```bash
ivyea config set chat_max_tool_steps 80
ivyea config set compact_at_tokens 96000
```

复杂任务也可以先创建长任务，再把对话绑定到它；一旦工具预算中断，会自动写入 task 日志和结构化 `resume` 信息，包含暂停原因、下一步、上一轮工具调用数量和可直接注入下一轮的续跑 prompt：

```bash
ivyea task create --title "补齐模型接入" --step "梳理 provider" --step "实现 OAuth" --step "测试发版"
ivyea chat --task-id <task-id>
ivyea task resume <task-id>
ivyea task continue <task-id> --message "从上次中断处继续"
curl -s http://127.0.0.1:8765/v1/tasks/<task-id>/resume
curl -s http://127.0.0.1:8765/v1/tasks/<task-id>/continue -H 'Content-Type: application/json' -d '{"message":"继续"}'
```

工程类对话会显示 `Code 计划 → 读上下文 → 修改/生成补丁 → 测试 → 复查` 阶段提示；工具调用会带 `1/48.1` 这样的进度编号，方便判断当前卡在读文件、跑测试还是生成补丁。终端默认使用轻量输入行，避免大块黑色输入/补全背景；喜欢框式输入可设置 `IVYEA_BOXED_INPUT=1`，不想要颜色可设置 `NO_COLOR=1`。

## 安装与部署

### 一键安装（推荐，装完任意目录敲 `ivyea`）
```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash
```
```powershell
# Windows PowerShell
iwr https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.ps1 -UseBasicParsing | iex
```
脚本会自动装好 pipx 并把 `ivyea` 装到 PATH。默认安装最新 GitHub Release wheel，失败时回退到 git main。国内慢可加镜像：`curl ... | PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash`。安装后会运行 `ivyea self doctor`；装完重开终端，`ivyea config` 即可。

常用变量：

| 变量 | 用途 |
| --- | --- |
| `IVYEA_VERSION=v1.0.19` | 固定安装某个 Release |
| `IVYEA_REF=main` | 从 git 分支/tag 安装 |
| `IVYEA_LOCAL=/path/to/repo` | 从本地源码安装 |
| `GITHUB_TOKEN=...` | 私有仓库读取 Release 资产 |
| `IVYEA_AUTO_INSTALL=0` | 禁止脚本自动安装 Python/pipx，只做检查 |
| `IVYEA_WHEELHOUSE=/path/to/wheelhouse` | 指定离线依赖目录 |

更完整的安装、升级、离线部署、企业内网部署见 [docs/部署指南.md](docs/部署指南.md)。

### 手动安装（pipx / venv）
跨平台纯 Python（≥3.9），依赖 pandas / openpyxl / httpx，**无需 Node/数据库**。

> 私有仓库阶段：从源码安装；转公开或发 PyPI 后可直接 `pipx install ivyea-agent` / `pipx install git+https://...`。

### Linux
```bash
sudo apt install -y python3 python3-pip pipx     # Debian/Ubuntu；CentOS 用 dnf
# 或 RHEL/Fedora: sudo dnf install -y python3 python3-pip pipx
pipx ensurepath          # 让 ~/.local/bin 进 PATH（首次后重开终端）
git clone https://github.com/Hector-xue/ivyea-agent.git
pipx install ./ivyea-agent
ivyea config             # 进配置向导
```

### macOS
```bash
brew install python pipx          # 没有 brew 就先装 Homebrew
pipx ensurepath
git clone https://github.com/Hector-xue/ivyea-agent.git
pipx install ./ivyea-agent
ivyea config
```

### Windows
1. 从 python.org 装 Python 3.9+，**勾选 “Add Python to PATH”**。
2. PowerShell：
```powershell
py -m pip install --user pipx
py -m pipx ensurepath        # 重开 PowerShell 生效
git clone https://github.com/Hector-xue/ivyea-agent.git
pipx install .\ivyea-agent
ivyea config
```
- 全部文件读写已用 UTF-8，中文不乱码；`config edit` 默认调用 notepad。
- 若搜索词报告 CSV 是 GBK（中文 Excel 导出），另存为 UTF-8 CSV 或 .xlsx 再喂给 `ivyea patrol`。

### 不想用 pipx（任意系统通用）
```bash
git clone https://github.com/Hector-xue/ivyea-agent.git && cd ivyea-agent
python3 -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
ivyea --version
```

### 升级 / 卸载
```bash
pipx upgrade ivyea-agent      # 源码装的：git pull 后 pipx reinstall ./ivyea-agent
pipx uninstall ivyea-agent
ivyea self status
ivyea self backup
ivyea self upgrade            # dry-run，输出升级计划
ivyea self uninstall          # dry-run，输出卸载计划
```

## 配置

**一条命令进交互式配置向导**（像 Hermes，逐项回车=保留当前；含密钥隐藏输入）：

```bash
ivyea config            # 交互向导：provider / 模型 / 站点 / 目标ACoS / API key
ivyea config show       # 查看当前配置
ivyea config set target_acos 0.3   # 单项设置
ivyea config edit       # 用 $EDITOR 直接编辑 .env
```

主脑模型 P1 用 DeepSeek（OpenAI 兼容、便宜、够用）；密钥存 `~/.ivyea/.env`（权限 600）。

配置模型（对标 Hermes 的 provider profile 思路：先看 provider，再看模型、认证和可用性）：
```bash
ivyea model                 # 交互选择模型并配置 key/base_url
ivyea model providers       # 查看 provider、认证方式、key 状态、能力标签、可用性
ivyea model doctor          # 检查当前主脑模型、认证状态和 provider 能力
ivyea model list            # 只列模型清单
ivyea model deepseek-chat   # 兼容旧 id：按别名直接切
ivyea model openrouter:anthropic/claude-sonnet-4.6
ivyea model ollama:qwen3-coder
ivyea model auth            # 查看 OAuth/Bearer provider 登录状态
ivyea model auth qwen-oauth --login
ivyea model auth qwen-oauth --device-code
ivyea model auth qwen-oauth --token <access_token>
ivyea model auth qwen-oauth --import-qwen-cli
ivyea model auth qwen-oauth --refresh
ivyea model auth qwen-oauth --probe
ivyea model auth openai-codex --device-code
ivyea model auth openai-codex --refresh
ivyea model auth openai-codex --probe
ivyea model auth google-gemini-cli --login
ivyea model auth google-gemini-cli --login --no-browser
ivyea model auth google-gemini-cli --project <gcp-project-id>
ivyea model auth google-gemini-cli --probe
ivyea model auth copilot --exchange
ivyea model auth copilot --probe
ivyea model logout qwen-oauth
```
已可用：Claude API、Gemini API、Gemini Code Assist OAuth 登录/token/项目保存/gcloud project 发现/真实 probe、AWS Bedrock Converse、OpenAI 兼容端点、OpenAI Codex OAuth Responses streaming、DeepSeek、通义千问、Kimi/Moonshot、Z.AI/GLM、豆包、MiniMax、OpenRouter、Nous(API key)、xAI、GitHub Copilot chat/completions、Qwen OAuth device-code/Qwen CLI 登录导入与 refresh token 自动刷新/真实 probe、Ollama/本地、自定义网关。
`model providers` 的能力标签含义：`tools` 工具调用、`stream` 流式、`vision` 多模态视觉、`models` 可刷新模型清单、`probe` 可发起真实可用性探测、`local` 本地端点。IvyeaOps 可直接读取 `/v1/model/providers` 构建模型设置页。
说明：`--token`、`--login`、`--device-code` 只代表本地保存凭证；`--probe` 才会发起真实请求验证模型、权限、配额和 transport，且不会打印 token。Gemini Code Assist `--probe` 会诊断 token、project、权限、配额和 onboarding 常见失败；Google 账号侧开通/配额调整仍需用户在 Google 控制台完成。Qwen OAuth device-code 已按官方 CLI 流程接入，但 Qwen 官方文档说明免费层已于 2026-04-15 停用，可能被服务端拒绝；生产建议使用 DashScope/Coding Plan/API key。这里不再写“Codex/Claude 会员登录”，因为 Hermes 对标的是 OAuth/API/专用 transport，不是普通网页会员登录。

### MCP 服务器（通用数据源；⚠️ 不是领星广告源）

> 更正（2026-06）：**领星广告数据不要走 MCP**——领星 MCP 无广告工具。领星请用上文
> `--from-lingxing`。本节的 MCP 客户端用于接入**其它**通用数据源（Sorftime/SIF/自建等）。
> 下文示例里凡出现 `领星` 仅为历史占位，实际请换成你的非领星 MCP 服务器名。

```bash
ivyea mcp add               # 对话式添加：名称 / 传输(http·sse·stdio) / URL / 鉴权(header·query)
ivyea mcp list
ivyea mcp tools <名称>       # 连上并列出该服务器暴露的工具(发现工具名/入参)
ivyea mcp call <名称> <工具> --args '{"asin":"B0.."}'   # 看某工具返回结构
ivyea mcp suggest <名称> <工具> --args '{"asin":"{asin}","days":"{days}"}'  # 根据返回自动建议 dataSource
ivyea mcp doctor            # 检查 transport/dataSource/writeActions 配置
ivyea mcp serve             # 反向 MCP：把 IvyeaAgent 作为只读 stdio MCP server 暴露给其它客户端
ivyea mcp self-config       # 输出其它 MCP 客户端可复制的 stdio 配置
ivyea mcp edit              # 编辑 mcp.json(填 dataSource 映射)
ivyea mcp remove <名称>
```

配置写入 `~/.ivyea/mcp.json`（权限 600，密钥只在你本机）。客户端是**通用的**——不绑死任何厂商工具名，由你配的 `dataSource` 映射驱动（approach c）。传输支持 `http`、`sse` 和本地 `stdio`。`ivyea mcp doctor` 会检查 transport、dataSource、writeActions、安全风险（例如带鉴权的明文 HTTP、stdio 命令缺失、疑似明文 token），并提示真实写入仍必须显式 `--execute` 且走审批/审计。

反向 MCP 默认只暴露只读工具：健康状态、manifest、知识检索、统一检索、skill 搜索、system doctor、task list/detail/resume、trace list/stats、workspace 搜索/巡检、code plan/context/bundle/repair。它不暴露 `execute_actions`、文件写入或命令执行。

**用 MCP 自动拉数（替代手动导 CSV）的步骤：**
1. `ivyea mcp add` 加好服务器（如领星）。
2. `ivyea mcp tools 领星` 看有哪些工具；`ivyea mcp call 领星 <工具> --args '{...}'` 看返回长什么样。
3. 先用 `ivyea mcp suggest 领星 <工具> --args '{"asin":"{asin}","site":"{site}","days":"{days}"}'` 生成 `dataSource` 建议；再 `ivyea mcp edit` 放进该服务器配置。
4. 仍可手工微调映射：
   ```json
   "dataSource": {
     "tool": "你的广告搜索词报告工具名",
     "args": {"asin": "{asin}", "site": "{site}", "days": 30},
     "rows_path": "data.rows",
     "field_map": {
       "Date":"date","ASIN":"asin","Campaign Name":"campaign","Match Type":"match_type",
       "Customer Search Term":"search_term","Impressions":"impressions","Clicks":"clicks",
       "Spend":"spend","Orders":"orders","Sales":"sales"
     }
   }
   ```
   （`{asin}/{site}/{days}` 运行时替换；`rows_path` 指向返回里行数组；`field_map` 左=规则引擎列，右=工具返回字段名。）
5. 跑巡检：
   ```bash
   ivyea patrol --from-mcp 领星 --asin B0XXXXXXXX --days 30
   ```

## 使用

```bash
ivyea patrol 搜索词报告.csv --asin B0XXXXXXXX --site US --target-acos 0.3
ivyea patrol 报告.csv --no-llm        # 只跑规则引擎，跳过 AI 复核
```

未配置模型 key 时会自动降级为「仅规则引擎」结论，不报错。报告同时保存为 `.md`。

## 对话模式（P2.5，推荐）

像跟 Claude Code 对话一样用它：自然语言 + 人工审批 + 斜杠命令。

```bash
ivyea                            # 直接敲 ivyea = 进对话模式（像 claude/hermes）
ivyea chat                       # 同上，dry-run 对话
ivyea chat --execute --from-mcp 领星 --protected "核心词,品牌词"   # 允许真写
```
- 直接说："看下 B0XXXXXXXX 这周广告"——Agent 自动 巡检→提动作→**逐条弹人工审批**(预览+`[1]是[2]本会话都允许[3]否[4]改[5]全停`)→执行→可回滚。
- 斜杠命令：`/help /model /mcp /tools /workspace /patch /gitops /clear /memory /exit`。
- 工程类问题会自动注入精简工程上下文：workspace 入口/测试/配置、git 状态和 skill 状态；运营类问题仍优先注入 Amazon skills 与知识库，避免上下文混杂。
- 写操作永远经人工审批，模型无法绕过；默认 dry-run。需配主脑模型 key（`ivyea config`）。

## 审核制执行（P2，命令式）

巡检给出建议后，用 `ivyea apply` 走**审核制执行**：逐条确认 → 经 MCP 写工具落地 → 审计可回滚。**默认 dry-run（只预览不写）**，真实执行需显式 `--execute --from-mcp`。

```bash
ivyea apply /巡检输出目录            # dry-run：列出否词/调价动作 + 护栏拦截，逐条确认
ivyea apply 巡检目录 --protected "我的核心词,品牌词"   # 指定保护词
ivyea apply 巡检目录 --execute --from-mcp 领星        # 真实执行（需配 writeActions）
ivyea audit list                     # 看执行记录
ivyea audit rollback <审计ID>        # 回滚某次写操作
```

**硬护栏（违反即阻断，不交给模型判断）**：不否品牌词/竞品词/核心品类词/保护词；置信度低不自动执行；单次调 bid ≤20%；小类目核心词不降 bid。

**写操作映射（approach c，配在 mcp.json 的服务器下）**：
```json
"writeActions": {
  "negative":          {"tool":"add_negative_keyword",    "args":{"keyword":"{search_term}","match_type":"{negate_match}"}},
  "negative_rollback": {"tool":"remove_negative_keyword", "args":{"keyword":"{search_term}"}},
  "bid":               {"tool":"update_bid",              "args":{"keyword":"{search_term}","bid":"{new_bid}"}},
  "bid_rollback":      {"tool":"update_bid",              "args":{"keyword":"{search_term}","bid":"{current_bid}"}}
}
```
> 调 bid 需要"当前 bid"才能算绝对值；搜索词报告里没有，所以 bid 动作默认作建议、不执行，除非另接 bid 数据。否词最干净，是 P2 主力。

## 记忆（P3）

Hermes 同款：**SQLite FTS5 + 策展 markdown + 自策展**，本地自有（不依赖向量库/GBrain），存 `~/.ivyea/memory.db`、`~/.ivyea/MEMORY.md`、`~/.ivyea/account/<ASIN>.md`。执行 `ivyea retrieval index` 时，这些记忆会和知识库一起进入持久化检索索引，供 CLI、本地 API 和 IvyeaOps 统一召回。

作用：
- **尊重历史否决**：你否过的否词/调价，下次巡检自动拦截、不再反复建议。
- **5 天稳定期**：刚调过 bid 的词，5 天内不重复调。
- **跨会话回忆**：`ivyea memory search <词>` / 对话里让它"回忆"。

```bash
ivyea memory                 # 状态 + 最近巡检
ivyea memory search 关键词    # 全文检索历史决策/巡检/要点
ivyea memory note B0XXXX     # 看某 ASIN 的运营记忆笔记
```
对话里：`/memory`、或直接说"记住…/回忆…"（remember / recall 工具）。

## Skills / 知识库（P3+）

Ivyea Agent 现在内置 **Amazon Skills**：把可复用运营流程从 prompt 里拆出来，按任务自动召回，类似 Claude/Codex 的 skill 工作流。

```bash
ivyea skill list                 # 列出内置/用户 skill
ivyea skill search 否词           # 搜索相关运营流程
ivyea skill show amazon.search_term_optimizer
ivyea knowledge audit             # 查看知识卡来源/可信度/更新时间
ivyea knowledge sources           # 来源登记表：source_url/type/license/category/card ids
ivyea knowledge watchlist         # Amazon 官方/社区来源观察清单；社区来源必须人工复核
ivyea knowledge plan ./note.md --id user.my-playbook --source-url https://... # 生成 diff 草案，不写入
ivyea knowledge apply ./note.md --id user.my-playbook --source-url https://... --confirm # 确认后写入并重建索引
ivyea knowledge import ./note.md --id user.my-playbook --tags "listing,预算"
ivyea knowledge url https://example.com/doc --source-type community --confidence medium
ivyea knowledge rebuild           # 校验并清理用户知识索引
ivyea knowledge index             # 重建 SQLite FTS/LIKE 搜索索引
ivyea knowledge conflicts         # 检查用户知识与官方知识的潜在冲突
ivyea eval                        # 业务质量回归：规则引擎/知识召回/skill召回/脱敏
```

当前内置：
- `amazon.search_term_optimizer`：搜索词分类、收割、控 bid、否词候选。
- `amazon.negative_keyword_guard`：否词误伤防护、exact/phrase 风险解释。
- `amazon.budget_pacing`：预算节奏、放量、出价冷却。
- `amazon.listing_conversion_audit`：广告词信号反推 Listing 主图/五点/A+ 转化问题。
- `amazon.launch_playbook`：新品期测词、收割、止损。
- `amazon.weekly_account_review`：周度账户复盘。

用户自定义 skill 放到 `~/.ivyea/skills/<domain>/<name>/`，包含 `skill.json` + `SKILL.md` 即可被发现。对话模式会按本轮问题自动注入最相关的 skill；也可用 `/skill <关键词>` 显式搜索。

用户知识卡存 `~/.ivyea/knowledge/`，来源清单在 `sources.jsonl`。导入时会记录 `source_type/confidence/retrieved_at/source_url/license/body_hash`，搜索和聊天注入会与内置知识一起召回。`knowledge sources` 会按来源聚合卡片，方便检查官方/社区/用户来源、缺失 URL 和 review_required。`knowledge watchlist` 只维护可采集来源，不自动抓论坛内容；`knowledge plan/apply` 用 diff 草案和确认写入流程，把官方文档摘要、社区经验和团队 SOP 分层纳入知识库。`knowledge index` 会构建 SQLite FTS5 索引（不可用时回退 LIKE），并写入 category/freshness/source_quality 等治理字段；`knowledge conflicts` 会标出用户知识和官方知识同标签但含反向表述的潜在冲突。

## Listing 转化诊断

广告数据里出现“高点击低转化/看似浪费”的词时，不应只看 ACoS 就否词。先用 Listing 诊断确认是不是标题、五点、A+、Review 或 Offer 承接问题：

```bash
ivyea listing audit \
  --title "Karaoke Machine" \
  --bullets "Portable Bluetooth Speaker" \
  --search-terms "karaoke machine kids,studio mic" \
  --rating 4.2 --review-count 18
```

对话模式也可以直接让 agent 调 `run_listing_audit`，它会输出搜索意图覆盖、转化风险、Listing 任务和广告动作建议。

## Review / Offer / 库存利润诊断

广告低转化也可能来自差评、Q&A 异议、价格、coupon、毛利或库存，而不是搜索词本身。Ivyea 提供两类确定性诊断：

```bash
ivyea review audit \
  --reviews "Poor quality, stopped working, missing cable" \
  --rating 3.8 --review-count 6 \
  --price 39.99 --competitor-price 29.99

ivyea offer audit \
  --price 39.99 --competitor-price 29.99 \
  --margin-rate 0.30 --target-acos 0.35 \
  --inventory-days 9 --spend 100 --sales 200
```

对话模式可调用 `run_review_audit` 和 `run_offer_audit`。输出会明确提示是否应该控预算/控 bid、先修复 Review/Offer，还是可以继续结合搜索词质量放量。

## 竞品关键词与周期复盘

```bash
ivyea competitor audit \
  --own-terms "karaoke machine,kids microphone" \
  --search-terms "acme karaoke,B0ABCDEFGH,wireless microphone" \
  --competitor-terms "acme" \
  --category-terms "microphone,karaoke"

ivyea weekly review
ivyea weekly review --output reports/weekly.md
```

`competitor audit` 会识别竞品词、ASIN 串号词、类目长尾扩展、保护词命中和缺失核心词。`weekly review` 聚合动作队列、记忆巡检、Scorecard、trace、影子台账，形成周期运营复盘。

## 自动预警与本地计划任务

```bash
ivyea alert check
ivyea alert check --notify --channel stdout
ivyea notify test --channel webhook --webhook-url https://example.com/hook --message "Ivyea Agent OK"
ivyea schedule set daily-alert alert --every-hours 24
ivyea schedule set daily-alert alert --every-hours 24 --notify --channel feishu
ivyea schedule set weekly-review weekly --every-hours 168
ivyea schedule set daily-eval eval --every-hours 24
ivyea schedule list
ivyea schedule run-due
```

`alert check` 会检查动作队列积压、已批准未执行、工具失败、影子台账待回测、画像缺失、用户知识库缺失和磁盘空间。`--notify` 可把结果发送到 `stdout`、通用 webhook 或飞书机器人；URL 可通过 `--webhook-url`、`IVYEA_NOTIFY_WEBHOOK_URL`、`IVYEA_FEISHU_WEBHOOK_URL` 或 settings 配置。`schedule` 只是本地计划注册表，不常驻；用 cron/systemd/IvyeaOps 定时调用 `ivyea schedule run-due` 即可。

## 本地安全策略

```bash
ivyea policy init
ivyea policy show
ivyea policy check-path ./reports --op write
ivyea policy check-command "git reset --hard"
ivyea policy explain-command "git push origin main"
```

`~/.ivyea/policy.json` 可配置文件读写目录、命令 allow/deny 和危险命令策略。默认不额外限制文件路径，但内置拒绝明显高风险命令；配置 `file_read_roots/file_write_roots` 后，`read_file/write_file/edit_file/list_dir` 会被限制在允许目录内。`explain-command` 会给出 allowed/risk/reasons，供执行前判断。

## 通用工程 Agent 能力

Ivyea Agent 的亚马逊能力是专业包，通用工程能力用于对标 Hermes/Codex/Claude Code 的本地项目工作流。

### Workspace 项目理解

```bash
ivyea workspace index --root .
ivyea workspace search "operate gate" --root .
ivyea workspace map --root .
ivyea workspace graph --root .
ivyea workspace inspect --root .
ivyea workspace explain ivyea_agent/cli.py --root .
```

`workspace` 会离线扫描文本文件，跳过 `.git/node_modules/dist/build/venv` 等噪声目录，索引保存到 `~/.ivyea/workspaces/`。它能输出语言分布、重要文件、目录结构、符号摘要、搜索命中、内部依赖图、外部依赖、入口点、测试文件、配置文件和建议命令，是后续代码理解、任务执行和 Review 的地基。`.github/workflows` 默认会纳入索引，用于 CI/发版判断。

### 长任务 Task Runner

```bash
ivyea task create --title "补齐通用能力" --step "索引项目" --step "任务编排" --workspace .
ivyea task list
ivyea task start <任务ID>
ivyea task step <任务ID> --index 1 --status completed --notes "已验证"
ivyea task resume <任务ID>
```

`task` 是本地长任务状态机，保存到 `~/.ivyea/tasks/`，用于记录计划、当前步骤、阻塞原因、日志和恢复提示。它和 `schedule` 不同：`schedule` 是定时任务注册表，`task` 是 Agent 执行复杂任务时的进度台账。

### Patch 修改闭环

```bash
ivyea patch make --path ivyea_agent/cli.py --old "旧文本" --new "新文本" --output patch.json
ivyea patch validate patch.json --root .
ivyea patch apply patch.json --root .                 # dry-run，显示 diff
ivyea patch apply patch.json --root . --execute       # 真实写入，需人工审批
ivyea patch tests --root .
ivyea patch run-tests --root . --command "python -m pytest tests/test_patcher.py"
```

`patch` 使用结构化 JSON 补丁，要求 `old` 文本在目标文件中精确且唯一匹配；默认只预览 diff，显式 `--execute` 才会写文件。它会经过 `~/.ivyea/policy.json` 的写路径检查，并复用人工审批。`tests` 会根据当前 git 变更建议测试命令，`run-tests` 可执行指定测试。

### Git/CI 工作流

```bash
ivyea gitops status --root .
ivyea gitops diff --root .
ivyea gitops workflows --root .
ivyea gitops ci --root . --limit 5
ivyea gitops release-plan --root . --version v1.0.18
ivyea gitops stage --root . --file ivyea_agent/cli.py
ivyea gitops stage --root . --file ivyea_agent/cli.py --execute
ivyea gitops commit --root . --message "Add patch workflow" --execute
ivyea gitops tag --root . --tag v1.0.18 --execute
ivyea codereview --root .
```

`gitops` 提供仓库状态、diff 摘要、GitHub Actions workflow 发现、远程 CI 最近运行状态、发版前检查，以及本地 `stage/commit/tag` 写操作。`ci` 优先使用已登录的 GitHub CLI，未登录时回退 GitHub API；私有仓库需要 `gh auth login` 或 `GH_TOKEN/GITHUB_TOKEN`。写操作默认只输出预览，`--execute` 才执行；不加 `--yes` 会弹人工审批。`codereview` 会扫描 git diff（含未跟踪新文件），识别疑似密钥、危险 shell、宽异常捕获、生产代码无测试改动等确定性风险。

对话模式里也可以直接用同样能力：

```text
/workspace map --root .
/workspace graph --root .
/workspace inspect --root .
/patch validate patch.json --root .
/gitops status --root .
/gitops ci --root .
```

### 用户 Skill

```bash
ivyea skill create general.release_check \
  --title "Release Check" \
  --trigger release \
  --tool gitops \
  --body "# Release Check\n\n检查 git 状态、测试和发版计划。"
ivyea skill status
ivyea skill audit
ivyea skill export-lock
```

用户 skill 存在 `~/.ivyea/skills/`，可以覆盖或扩展内置 Amazon skills。`skill status` 会显示当前生效版本、用户覆盖、内置/用户版本差异和知识卡缺失；`skill audit` 会检查触发词、正文和关联知识卡是否缺失；`skill export-lock` 会导出当前生效 skill 集，方便团队复现同一套能力包。

### 通用多模态视觉

```bash
ivyea vision ./screenshots --task "检查手机端是否横向滚动或元素重叠" --provider openai --payload
ivyea vision ./reports --task "识别报表里的异常指标和下一步动作" --provider anthropic --output reports/vision.json
```

`vision` 是通用截图/UI/报表入口；`image vision` 仍保留为 Listing 图片专用入口。默认只生成 OpenAI/Claude/Gemini 请求包，显式 `--call` 才会调用外部模型。

## 图片视觉诊断与多模态模型

视觉识别建议分三层：

1. **本地资产预检查**：不花模型费，先扫图片数量、尺寸、比例、文件体积、命名角色（主图/尺寸/场景/卖点/对比）。
2. **OCR/规则检查**：后续可接 OCR，检查图片中文字、尺寸标注、卖点覆盖、合规风险。
3. **多模态大模型**：把图片和结构化 prompt 交给 GPT/Claude/Gemini 这类视觉模型，让它判断主图点击力、场景图承接、A+ 信息结构、视觉信任问题。

当前已落地第一层，并能导出第三层需要的 prompt：

```bash
ivyea image audit ./listing-images --prompt
ivyea image ocr ./listing-images --lang eng+chi_sim
ivyea image audit ./listing-images --prompt --prompt-out reports/image-vision-prompt.md --context "karaoke machine, US, target ACOS 30%"
ivyea image vision ./listing-images --provider openai --payload --output reports/openai-vision.json
OPENAI_API_KEY=... ivyea image vision ./listing-images --provider openai --call
ivyea image vision ./listing-images --provider anthropic --output reports/claude-vision.json
ivyea image vision ./listing-images --provider gemini --output reports/gemini-vision.json
```

Agent 对话中可调用 `run_image_audit`。`image vision` 默认生成 provider-neutral 的视觉请求包：OpenAI Responses 形态、Anthropic Messages 形态、Gemini contents 形态；只有显式传 `--call` 才会调用外部多模态 API。密钥读取 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GEMINI_API_KEY`，也可用 `--api-key` 临时覆盖。真正调用多模态 API 时，建议使用“本地预检查 + 多模态审核 + 人工确认”的三段式，避免模型看错图片文字后直接改广告策略。

OCR 通过本机 `tesseract` CLI 可选启用：未安装时会提示安装，不影响其它诊断；已安装时 `ivyea image ocr` 可识别图片文字并自动脱敏。

## 领星 OpenAPI 店铺巡检（真实广告数据，推荐）

> ⚠️ 重要更正（2026-06）：**领星 MCP 没有广告工具**（只有 ERP/库存/利润）。真实广告
> 数据走**领星 OpenAPI**。agent 已独立实现领星 OpenAPI（鉴权/签名/拉数），并移植了
> ivyea-ops 的 sid 维度确定性规则引擎（五杠杆 + 毛利率推目标ACOS）。

```bash
ivyea lingxing setup        # 配 host/appid/secret（只存本机 ~/.ivyea/）
ivyea lingxing probe        # 自检：取令牌 + 拉店铺列表
ivyea lingxing sellers      # 列店铺，拿 sid
ivyea patrol --from-lingxing --sid 1863 --days 30   # 店铺维度只读巡检
```
窗口逐日聚合（丢最近 N 天归因），按否词/收割/降bid/加bid/加预算分区出候选，每条带规则+
指标+理由，历史否决/冷却自动拦截。

### 写入执行（审批制，默认 dry-run）

```bash
ivyea patrol --from-lingxing --sid 1863 --days 30 --execute   # 巡检 + 逐条人工审批
ivyea lingxing operate status      # 看写入总开关（默认 关）
ivyea lingxing operate on          # 开启真写（默认 120 分钟后自动关）
ivyea lingxing operate off         # 关回 dry-run
ivyea audit list                   # 看写入审计
ivyea audit rollback <审计ID>      # 一键回滚（否词→归档；调bid/预算→还原旧值）
```
写入支持：**否词 / 关键词调bid / 活动预算**（收割为建议项，不自动写）。三重硬闸：
① 每条**人工逐条审批**（`[1]是 [2]本会话都允许 [3]否 [4]改 [5]全停`）；② **operate 开关**默认关，
不开就只 dry-run；③ **幅度 ≤±20%** 硬闸 + 历史否决/冷却拦截。写前抓快照，失败自动熔断关开关。
请求体/路由/回滚逐字段对齐领星官方 + ivyea-ops 生产实现。

## 设计 / 路线图

- 总纲：对标 Hermes/Codex/Claude Code 的完整方案见 `/root/ivyea-agent-优化方案-对标三大产品.md`。
- LLM Provider 层：**OpenAI 兼容（DeepSeek 等）+ Claude 原生（官方 SDK，含 prompt caching）+ Gemini 原生 generateContent + Gemini Code Assist OAuth 登录/token/probe 诊断 + AWS Bedrock Converse + GitHub Copilot chat/completions + OpenAI Codex OAuth Responses streaming 均可用**；模型配置已改为 Hermes 风格 provider profile：`ivyea model providers` 看 OpenAI/Codex/Claude/Gemini/Bedrock/DeepSeek/Qwen/Kimi/Z.AI/豆包/MiniMax/OpenRouter/Nous/xAI/Copilot/Ollama/自定义等 provider。Qwen OAuth 支持官方 device-code 流程和 Qwen CLI 登录导入到 `~/.ivyea/auth.json`，并在 access token 临近过期时自动刷新；Gemini Code Assist 可浏览器 OAuth 登录或导入 Google OAuth access/refresh token 调用 cloudcode-pa。
- 数据源：① 本地 CSV（vendored 规则引擎，离线/试用兜底）② **领星 OpenAPI 店铺维度（真实广告链路）** ③ 通用 MCP（任意数据源，**非领星广告**）。
- 记忆：SQLite FTS5 + 策展 markdown（中文回退 LIKE 子串检索）；**会话转录回忆 + 压缩摘要入库 + 自策展 nudge + 持久指令注入（USER.md/AGENTS.md，CLAUDE.md 同款，`/init` 生成）** 均已落地，已对真实 DeepSeek 验证指令被遵守。

## 状态（诚实盘点 2026-06）

**可用**：CSV 只读巡检；领星 OpenAPI 店铺维度巡检（真链路已实测：令牌+11店+报表 code=0）；**领星写入执行（否词/调bid/预算 + 审批 + operate开关 + 幅度闸 + 审计回滚）**——dry-run 与请求体已端到端验证，逻辑由 41 项 pytest 覆盖；通用 MCP 客户端；权限审批引擎；SQLite 记忆。

**对话式内核**：**流式输出 + 多步工具循环 + 成本/token 核算 + 计划模式 + 可控上下文压缩 + 会话 resume + 终端 Markdown 渲染**均已实现并对真实 DeepSeek 验证（含 prompt caching 实测命中）。聊天命令：`/plan`/`/approve`（计划模式）、`/cost`（用量）、`/compact`（手动压缩上下文）、`/compact auto on|off`（控制自动压缩）、`/raw`（切原始流式）。续接会话：`ivyea --continue` 或 `ivyea chat --resume [<id>]`。默认不自动压缩，避免长任务中途被打断。

**通用工具能力**：除广告巡检外，agent 还能 `read_file`/`list_dir`/`web_fetch`/`web_search`（只读自动放行）、`write_file`/`edit_file`/`run_python`（可用 pandas/openpyxl 读 Excel、算数）/`run_command`（写/执行经人工审批 + 计划模式拦截 + 沙箱限工作目录/超时/输出截断）。高风险命令（如 `git reset --hard`、`rm -rf /`）会被安全策略拒绝；`policy explain-command` 会给出风险分级；工具展示和 trace 会自动脱敏 API key/token/secret。

**通用工程 Agent 能力**：已新增 `workspace`（本地项目索引/搜索/地图/依赖图/入口与测试巡检/解释）、`task`（长任务状态机/步骤/日志/resume）、`code bundle`（计划/上下文/影响面/测试/续跑提示任务包）、`patch`（结构化补丁校验/应用/测试建议）、`gitops`（Git 状态/diff/workflow/CI 状态/发版检查/stage/commit/tag）、`codereview`（确定性 diff 风险审查）、`skill create/audit/status/export-lock`（用户 skill 脚手架/审计/版本状态/锁定导出）和通用 `vision`（截图/UI/报表多模态入口）。这些是对标 Codex/Claude Code 的通用工程底座，写操作默认 dry-run 并经过人工审批。

**生命周期管理**：`ivyea self status/backup/upgrade/uninstall` 提供安装方式识别、用户数据备份、升级计划和卸载计划。升级/卸载默认 dry-run，显式 `--execute` 才执行，并复用人工审批。

**视觉（对标 Claude Code）**：长任务用 `todo_write` 渲染 **Todo/Plan 面板**（☑/◐/☐ 实时勾选，真实 DeepSeek 已驱动跑通）；文件改动审批显示**彩色 diff**（红删绿增）；状态栏显示 模型/计划模式/上下文 token/累计 ¥；终端 Markdown 渲染；`NO_COLOR` 环境变量去色。

**体验/采用**：首次运行 **引导向导**（`ivyea onboard`：选模型→配 key→可选领星→AGENTS.md）；领星巡检带 **SQLite 缓存**（报表 7 天/实时 30 分，重复巡检秒回、尊重 1/s 限流，`ivyea lingxing cache clear` 清）+ **逐日进度条**；对话 **Ctrl+C 中断本轮不杀会话**；**每日成本护栏**（`config set daily_cost_limit_cny <元>`，超限暂停问人，`/cost` 看当日）；`run_command` 跨平台（Windows cmd / *nix bash）。运行时间线可用 `ivyea trace` 查看，Scorecard 会汇总工具调用次数/失败数/耗时。

**健壮性**：provider 对 **429/5xx/网络错误自动重试+指数退避**（4xx 不重试，直接抛）；**降级链**——主脑重试后仍挂/无额度时，自动切到 `config set fallback_models <id,…>` 配的备用模型继续（流式仅在尚未吐字前可安全降级）。Anthropic 走官方 SDK 自带重试。

**影子模式（护城河，三大产品没有）**：动钱前先用数据换信任。`ivyea shadow on` 后巡检**只记建议、不写广告**；每次巡检把候选记进影子台账（含触发指标），过几天 `ivyea shadow report --sid <SID>` 用**后续真实花费回测**——否词的词之后还烧了多少钱(0单)=若照做能省的钱，收割的词之后又出了多少单=若照做抓住的增量。报告诚实：后来转化了的"否词"不计入节省。新用户先开影子模式攒收益、看准了再让它真动手。

**工程化**：pytest 覆盖规则引擎、写入护栏、skills、知识库、trace、安全脱敏、画像、业务 eval；`ivyea eval` 固化关键质量回归（广告决策 golden、知识召回、skill 召回、安全脱敏）+ ruff lint + **GitHub Actions CI**（ubuntu/macos/windows × py3.9/3.11/3.12，三平台全绿；测试命令执行已做跨平台守卫，不再依赖 *nix 专有 `bash -lc`）+ **打 tag 即发布**（构建 sdist、wheel 和离线部署包挂到 GitHub Release）。当前文档按 **v1.0.19** 示例维护。

**尚未实现 / 待办**（不再标“✅完成”）：
- 领星**真实写入**仅 dry-run/单测验证，**尚未在生产真按下写**（需活跃广告店 + 你授权开 operate 实测）。
- Claude 原生已接（格式翻译+流式+caching，请求被 Anthropic 接受），**成功响应未实测**（需 ANTHROPIC_API_KEY）。
- Google 控制台侧的 Gemini Code Assist 开通/配额调整无法由 CLI 自动代办；当前通过 `--probe` 做可读诊断。
- 嵌入 IvyeaOps（内核成库 + 反向 MCP）。
- （可选）发 PyPI：当前发布走 GitHub Release（git/wheel 可装），需要 PyPI 时再配可信发布。

路线（总纲 M0–M7）：M0 止血+核实 ✅ → M1 领星适配层（只读）✅ → M2 领星写入执行+审批 ✅ → M1+ 内核(流式/成本/Plan) ✅ → M1++ 上下文压缩/resume/Markdown ✅ → 模型层(Claude 原生+caching) ✅ → 工具能力(文件/执行/web+沙箱+门控) ✅ → 记忆(回忆/摘要入库/自策展/持久指令) ✅ → 交互/视觉(Todo面板/彩色diff/状态栏) ✅ → 工程化(CI/eval/打包) ✅ → 体验(缓存/引导/中断/成本护栏) ✅ → 健壮性(重试/降级链) ✅ → 影子模式(护城河) ✅ → 嵌入 IvyeaOps。
