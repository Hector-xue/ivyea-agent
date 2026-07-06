# Ivyea Agent · 自托管亚马逊运营 CLI Agent

[![Release](https://img.shields.io/github/v/release/Hector-xue/ivyea-agent?label=release)](https://github.com/Hector-xue/ivyea-agent/releases/latest)
[![Stars](https://img.shields.io/github/stars/Hector-xue/ivyea-agent?style=flat&logo=github)](https://github.com/Hector-xue/ivyea-agent/stargazers)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)

**Ivyea Agent** 是一个**开源、自托管**的亚马逊运营命令行智能体：把搜索词 / 广告数据变成**可执行、可审计**的优化动作——**确定性规则引擎打底、LLM 只做复核**，所有写操作走**审核制 + 护栏**，数据与密钥全部留在你自己的机器。

它同时内置对话式交互、通用代码工程、长期记忆、Skill / 知识库与多模型接入，既能当亚马逊运营助手，也能当你的本地终端 Agent。纯 Python（≥3.9），无需 Node / 数据库，装完任意目录敲 `ivyea` 即用。

> 设计哲学：**证据驱动 + 标签化结论、确定性护栏不交给模型判断、写操作永远经人工审批、数据私有**。

- **门户网站**：<https://agent.ivyea.com>
- **仓库**：<https://github.com/Hector-xue/ivyea-agent>
- **最新 Release**：`v1.4.1`（`main` 分支可能包含尚未打包的新改动）

---

## 交流与反馈

欢迎扫码加入微信群，反馈 Bug、交流 Ivyea Agent 使用经验、AI 工具与亚马逊运营相关知识。**也欢迎提改进建议**——功能需求、交互优化、文档纠错都行，可在群里直接说，或到 GitHub 提 [Issue](https://github.com/Hector-xue/ivyea-agent/issues) / PR。群二维码可能会过期；如果扫码失效，可先关注公众号，再获取最新群二维码。

<table>
  <tr>
    <td align="center" width="50%">
      <img src="docs/assets/wechat-group-qr.png" alt="Ivyea 微信交流群二维码" width="300" />
      <br />
      <strong>微信群：Ivyea 的精神股东们</strong>
      <br />
      <sub>反馈 Bug / 交流 AI 与运营 / 提改进建议</sub>
    </td>
    <td align="center" width="50%">
      <img src="docs/assets/wechat-official-account-qr.jpg" alt="Ivyea 公众号二维码" width="220" />
      <br />
      <strong>公众号</strong>
      <br />
      <sub>群二维码失效时，关注后获取最新版</sub>
    </td>
  </tr>
</table>

---

## 目录

- [核心特性](#核心特性)
- [三分钟安装](#三分钟安装)
- [广告优化闭环](#广告优化闭环核心)
- [对话模式](#对话模式)
- [配置模型与登录](#配置模型与登录)
- [代码工程能力](#代码工程能力)
- [记忆与检索](#记忆与检索)
- [Skill 与知识库](#skill-与知识库)
- [MCP 互操作](#mcp-互操作)
- [程序化 / 无人值守运行](#程序化--无人值守运行)
- [嵌入 IvyeaOps](#嵌入-ivyeaops)
- [安全与护栏](#安全与护栏)
- [配置与数据目录](#配置与数据目录)
- [文档](#文档)

---

## 核心特性

- **本地私有**：跑在你自己的机器，业务数据、API 密钥都存 `~/.ivyea/`（权限 600），不出私域、不绑第三方云。
- **规则引擎 + LLM 复核**：广告决策由确定性规则引擎产出，LLM 只做词分类 / 归因 / 护栏检查 / 措辞，不推翻数据；没配模型也能只跑规则引擎。
- **写操作审核制**：所有会动钱、动文件、动命令的操作默认 dry-run，逐条弹人工审批，可审计、可回滚；确定性硬护栏（幅度上限 / 保护词等）不交给模型判断。
- **影子模式**：真动手前先用数据换信任——只记建议不写广告，几天后用后续真实花费回测「照做能省多少 / 抓住多少增量」。
- **对话即工具**：自然语言 + 斜杠命令，广告巡检、代码工程、记忆、Skill、MCP 全在一个对话里自主编排。
- **多模型可选**：OpenAI 兼容（DeepSeek 等）、Claude、Gemini、国产模型、订阅 OAuth 登录随意切，带自动重试 + 降级链。
- **可深度思考、会自我校验**：可调思考深度、完成前自动跑校验门禁、支持自我批判复核。
- **跨平台**：Linux / macOS / Windows 全支持，纯 Python，无需 Node / 数据库。

---

## 三分钟安装

**Linux / macOS**

```bash
curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | bash
ivyea config      # 交互配置：模型 / 站点 / 目标 ACoS / API key
ivyea chat        # 进对话
```

**Windows PowerShell**

```powershell
iwr https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.ps1 -UseBasicParsing | iex
ivyea config
ivyea chat
```

一键脚本会自动装好 pipx、把 `ivyea` 装进 PATH，默认安装最新 Release wheel（不可用时回退 git 源码），装完自动跑 `ivyea self doctor` 自检。缺基础环境时会尽量自动补齐（Linux `apt/dnf/yum/apk`、macOS Homebrew、Windows winget）。

<details><summary>固定版本 / 镜像加速 / 手动安装</summary>

```bash
# 固定某个 Release
curl -fsSL https://raw.githubusercontent.com/Hector-xue/ivyea-agent/main/scripts/install.sh | IVYEA_VERSION=v1.4.1 bash
# 国内镜像加速
curl -fsSL .../install.sh | PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash
# 手动（pipx）
git clone https://github.com/Hector-xue/ivyea-agent.git
pipx install ./ivyea-agent && ivyea config
```

常用环境变量：`IVYEA_VERSION`（固定 Release）、`IVYEA_REF`（从分支/tag 装）、`IVYEA_LOCAL`（本地源码）、`GITHUB_TOKEN`（私有仓库）、`IVYEA_AUTO_INSTALL=0`（只检查不自动装环境）、`IVYEA_WHEELHOUSE`（离线依赖目录）。

团队 / 离线环境可在发版机 `python scripts/build_offline_bundle.py` 生成含 `wheelhouse/` 的离线包，用户解压后 `bash install.sh` 即装，不再下载依赖。完整安装、升级、离线与内网部署见 [docs/部署指南.md](docs/部署指南.md)。
</details>

---

## 广告优化闭环（核心）

一条数据 → 一份可执行、可审计的优化方案。数据源三选一：

| 数据源 | 命令 | 说明 |
|---|---|---|
| **本地 CSV / xlsx** | `ivyea patrol 搜索词报告.csv --asin B0XX --site US --target-acos 0.3` | 离线 / 试用兜底 |
| **领星 OpenAPI**（真实广告链路，推荐） | `ivyea patrol --from-lingxing --sid 1863 --days 30` | 店铺维度，令牌 + 签名 + 拉数已内置 |
| **通用 MCP**（Sorftime / 自建等，非领星广告） | `ivyea patrol --from-mcp <服务器> --asin B0XX --days 30` | 由你配的 `dataSource` 映射驱动 |

输出一份**只读巡检报告**：否词候选 / 放量 / 降 bid / 加 bid / 加预算 / Listing 反馈 / 观察 / 人工复核，每条带**规则 + 指标 + 理由 + 置信度**，历史否决与冷却期自动拦截。**默认不会自动改广告。**

**审批制写入**（默认 dry-run，真实执行需显式开启）：

```bash
ivyea patrol --from-lingxing --sid 1863 --days 30 --execute   # 巡检 + 逐条人工审批
ivyea lingxing operate on          # 开启真写（默认 120 分钟后自动关）
ivyea audit list                   # 写入审计
ivyea audit rollback <审计ID>      # 一键回滚（否词→归档；调 bid/预算→还原旧值）
```

三重硬闸：① 每条**人工逐条审批**（`[1]是 [2]本会话都允许 [3]否 [4]改 [5]全停`）；② **operate 总开关**默认关，不开只 dry-run；③ **幅度 ≤±20% 硬护栏** + 历史否决 / 冷却拦截。写前抓快照，失败自动熔断关开关。支持：否词 / 关键词调 bid / 活动预算（收割为建议项，不自动写）。

**硬护栏（违反即阻断，不交给模型判断）**：不否品牌词 / 竞品词 / 核心品类词 / 保护词；置信度低不自动执行；单次调 bid ≤20%；小类目核心词不降 bid。

### 影子模式（先攒收益，看准了再动手）

```bash
ivyea shadow on                       # 只记建议、不写广告
ivyea shadow report --sid <SID>       # 用后续真实花费回测收益
```

每次巡检把候选记进影子台账（含触发指标），过几天用**后续真实花费回测**：被否的词后来还烧了多少钱（0 单）= 照做能省的钱；收割的词后来又出了多少单 = 照做抓住的增量。后来转化了的「否词」不计入节省——报告只认诚实收益。

### 配套确定性诊断

低转化不一定是搜索词的错。以下诊断帮你先分清是 Listing、Review、Offer 还是库存利润问题（对话里可直接让 agent 调用对应工具）：

```bash
ivyea listing audit --title "..." --bullets "..." --search-terms "..." --rating 4.2 --review-count 18
ivyea review audit  --reviews "..." --price 39.99 --competitor-price 29.99
ivyea offer audit   --price 39.99 --margin-rate 0.30 --inventory-days 9 --spend 100 --sales 200
ivyea competitor audit --own-terms "..." --search-terms "..." --category-terms "..."
ivyea weekly review   # 周度账户复盘：聚合动作队列 / 记忆 / Scorecard / trace / 影子台账
```

---

## 对话模式

像用终端助手一样用它：自然语言 + 人工审批 + 斜杠命令。

```bash
ivyea            # 直接敲 = 进对话
ivyea chat --execute --from-lingxing --protected "核心词,品牌词"   # 允许真写
```

- 直接说「看下 B0XXXX 这周广告」——agent 自动 **巡检 → 提动作 → 逐条弹审批 → 执行 → 可回滚**。
- 运营类问题自动注入相关 **Amazon Skill 与知识库**；工程类问题注入 **workspace 入口 / 测试 / git 状态**，上下文不混杂。
- 斜杠命令：`/help /model /think /critique /mcp /tools /workspace /patch /gitops /plan /approve /compact /memory /rewind /clear /exit`。
- 终端体验：带框输入区钉底、逐字流式、Markdown 渲染、彩色 diff、Todo/Plan 面板、状态栏显示 模型 / 模式 / 上下文 token / 累计花费；`Ctrl+C` 中断本轮不杀会话；`NO_COLOR=1` 去色、`IVYEA_BOXED_INPUT=0` 退回单行输入。

**思考深度与自我校验**：

```bash
/think high            # 调思考深度：off|low|medium|high|auto（推理型主脑生效）
/critique              # 对最近一次回答做 rubric 自我批判复核
```

写过源码的对话轮在收尾前会**自动跑完成前自验证门禁**（确定性代码审查 + 针对改动的 focused 测试），有高危问题或测试失败就先逼修复再收尾（`ivyea config set verify_before_done false` 可关）。

**上下文与长任务**：长任务默认尽量跑完整，不为省 token 自动压缩；`/compact` 手动压缩。工具单轮上限默认 48 步，可 `ivyea config set chat_max_tool_steps 80` 调高。复杂任务可绑长任务，工具预算中断时自动写入结构化 `resume`，随时续跑：

```bash
ivyea task create --title "补齐模型接入" --step "梳理 provider" --step "实现 OAuth" --step "测试发版"
ivyea chat --task-id <task-id>
ivyea task resume <task-id>
```

---

## 配置模型与登录

先看 provider，再看模型、认证与可用性；密钥存 `~/.ivyea/.env`（权限 600）。

```bash
ivyea config                # 交互向导：provider / 模型 / 站点 / 目标 ACoS / API key
ivyea model                 # 交互选择模型并配 key/base_url
ivyea model providers       # 看 provider、认证方式、key 状态、能力标签、可用性
ivyea model doctor          # 检查当前主脑、认证状态和 provider 能力
ivyea model deepseek-chat   # 按别名直接切
ivyea model openrouter:anthropic/claude-sonnet-4.6
```

**可用 provider**：OpenAI 兼容端点（DeepSeek / 通义千问 / Kimi·Moonshot / Z.AI·GLM / 豆包 / MiniMax / OpenRouter / Nous / xAI / Ollama·本地 / 自定义网关）、Claude（API key 及**订阅 OAuth 登录**）、Gemini（API + Code Assist OAuth）、AWS Bedrock Converse、GitHub Copilot、OpenAI Codex（OAuth）、Qwen（OAuth）。能力标签：`tools` 工具调用、`stream` 流式、`vision` 视觉、`probe` 真实探测、`local` 本地端点。

**登录制 provider**（OAuth / Bearer）：

```bash
ivyea model auth                          # 看各 provider 登录状态
ivyea model auth anthropic-oauth --login  # Claude 订阅登录（授权后粘回 code）
ivyea model auth anthropic-oauth --probe  # 真实请求验证 token 可用
ivyea model auth openai-codex --device-code
ivyea model auth google-gemini-cli --login
```

`--login` / `--device-code` / `--token` 只在本机保存凭证；`--probe` 才发起真实请求验证模型 / 权限 / 配额，且不会打印 token。**健壮性**：provider 对 429/5xx/网络错误自动重试 + 指数退避；主脑挂了自动切到 `config set fallback_models <id,…>` 的备用模型继续。

---

## 代码工程能力

Ivyea Agent 内置一套完整的本地代码工作流：先理解项目，再规划修改，再用结构化 patch + 测试 + 审查门禁收口。CLI 与对话里都能用，写操作默认 dry-run 且经人工审批。

| 能力 | 命令 | 说明 |
|---|---|---|
| **项目理解** | `ivyea workspace index / search / map / graph / explain` | 离线索引，输出语言分布、依赖图、入口、测试、符号 |
| **规划 / 上下文** | `ivyea code plan / context / bundle / brief` | 任务拆解、相关文件、影响面、紧凑上下文 |
| **结构化补丁** | `ivyea patch validate / apply --execute` | JSON 补丁精确唯一匹配，默认预览 diff |
| **测试 / 修复闭环** | `ivyea code test / repair / apply-loop` | 跑测试 → 解析失败 → 生成修复计划 |
| **Git / CI** | `ivyea gitops status / diff / ci / stage / commit / tag` | 仓库状态、CI 运行、发版前门禁、受控写 |
| **确定性审查** | `ivyea code review` / `codereview` | 扫 diff：疑似密钥、危险 shell、宽异常、无测试改动 |

对话里同样可用代码导航（`grep` / `code_search` / `code_symbols` / `code_impact`）、结构化改代码闭环（`code_apply_patch` → `run_tests` → `code_repair`）、只读并行**子 agent**（`dispatch_subagent` 铺开多角度调研、不污染主线）、以及**完成前自验证门禁**。写文件需要 `--execute`，提交 / 推送 / 发版永远不会被自动触发。

---

## 记忆与检索

本地自有的长期记忆：**SQLite FTS5 + 策展 markdown + 自策展**，不依赖向量库，存 `~/.ivyea/memory.db`、`~/.ivyea/MEMORY.md`、`~/.ivyea/account/<ASIN>.md`。

- **尊重历史否决**：你否过的否词 / 调价，下次巡检自动拦截、不再反复建议。
- **稳定期**：刚调过 bid 的词，冷却期内不重复调。
- **跨会话回忆**：`ivyea memory search <词>`，或对话里直接说「记住… / 回忆…」。

```bash
ivyea retrieval index                 # 把知识库 + 记忆写入本地持久检索索引
ivyea retrieval search "高点击 零单 是否否词"
```

默认索引后端 `local_hash_embedding_v1` 不依赖外部向量库，开箱可用；需要本地 dense embedding 时可装 `pip install "ivyea-agent[semantic]"` 并配 `sentence-transformers` 模型，失败自动降级回 hash 索引。

---

## Skill 与知识库

把可复用运营流程从 prompt 里拆出来，按任务自动召回。

```bash
ivyea skill list / search 否词 / show amazon.search_term_optimizer
ivyea skill create general.release_check --title "..." --trigger release --tool gitops --body "..."
```

内置 Amazon Skill：搜索词优化、否词误伤防护、预算节奏、Listing 转化审计、新品期打法、周度复盘。用户 skill 放 `~/.ivyea/skills/<domain>/<name>/`（`skill.json` + `SKILL.md`）即被发现，可覆盖内置。

知识库支持风险路由、回答内 `[K#]` 引用、来源登记、时效 / 许可审计、冲突检查，以及 diff 草案确认写入。公开官方源只做增量监控并进入审核队列；Seller Central 登录后内容只能通过授权导出导入，不绕过登录：

```bash
ivyea knowledge audit / sources / watchlist / conflicts
ivyea knowledge official-sources                         # 官方来源、权威层级、站点和更新策略
ivyea knowledge sync --force                            # 检查公开官方源；不自动发布
ivyea knowledge changes                                 # 查看待审核变更
ivyea knowledge review <event-id> --decision approved --confirm # 只批准进入导入草案，不自动发布
ivyea knowledge review-history [event-id]               # 不可变审核历史
ivyea knowledge versions [user.card-id]                  # 用户知识不可变版本账本
ivyea knowledge rollback user.card-id --id kv-... --confirm # 回滚后生成新版本并重建索引
ivyea knowledge governance                              # 审核/时效/覆盖/冲突总看板
ivyea knowledge coverage                                # 关键知识域 × marketplace 缺口
ivyea knowledge freshness                               # 知识卡与来源监控时效
ivyea knowledge quality                                 # 运行数据化持续评测集
ivyea knowledge evidence-schema                         # 授权账户证据 JSON Schema
ivyea knowledge evidence-plan docs/examples/knowledge-evidence.json
ivyea knowledge evidence-apply ./evidence.json --confirm # 专项脱敏、确认后入库；不保存原始文件
ivyea knowledge ads-capabilities                       # 广告产品/报表/归因能力与动态边界
ivyea knowledge ads-analyze docs/examples/amazon-ads-report.json
ivyea knowledge ads-analyze docs/examples/amazon-traffic-experiment.json
ivyea knowledge plan ./note.md --id user.my-playbook --source-url https://...   # 生成 diff 草案，不写入
ivyea knowledge apply ./note.md --id user.my-playbook --confirm                 # 确认后写入并重建索引
ivyea schedule set knowledge-quality knowledge_quality --every-hours 24
```

广告分析只计算有明确定义的 CTR/CPC/CVR/ACoS/ROAS；零分母返回空值。报表必须保留产品、类型、窗口、时区、币种、归因和销售范围，流量实验会检查变更隔离、窗口可比性和混杂因素，并始终阻断“账户现象＝官方算法”。完整路线见 [Amazon 专业知识库推进方案](docs/Amazon专业知识库推进方案.md)。

---

## MCP 互操作

**作为客户端**接入任意 MCP 数据源（协议覆盖 tools + resources + prompts，http / sse / stdio）：

```bash
ivyea mcp add / list / tools <名称> / call <名称> <工具> --args '{...}'
ivyea mcp doctor            # 检查 transport / dataSource / writeActions / 安全风险
```

配置写 `~/.ivyea/mcp.json`（权限 600），由你配的 `dataSource` 映射驱动、不绑死厂商工具名。对话里 agent 能自主 `mcp_list_tools` / `mcp_call_tool` 等：计划模式拒绝写、标 `"trusted": true` 免审、否则逐次审批。

**反过来 Ivyea Agent 也能作为只读 MCP server**（`ivyea mcp serve`），把知识卡暴露为 resources、Skill 暴露为 prompts、只读能力暴露为 tools，供其它 MCP 客户端接入；不暴露任何写 / 执行能力。

---

## 程序化 / 无人值守运行

`-p` 一次性运行可当真正的程序化 runner：

```bash
ivyea chat -p "分析 B0XXXX 广告并给方案" --output-format stream-json   # 逐行 NDJSON 事件
ivyea chat -p "..." --resume <session_id>                              # 续接多轮会话
ivyea chat -p "..." --permission-mode policy                          # 按 ~/.ivyea/policy.json 自动判定
```

`--output-format stream-json` 逐行输出结构化事件（`system/init` → `assistant` → `user`(tool_result) → `result`，费用字段 `total_cost_cny`），程序侧可直接可视化工具调用。`policy` 档在无人值守下按策略自动放行/拒绝，单工具拒绝不终止整轮。配合 cron / systemd 可做定时预警与复盘：

```bash
ivyea alert check --notify --channel feishu     # 队列积压 / 失败 / 画像缺失等预警
ivyea schedule set weekly-review weekly --every-hours 168
ivyea schedule set amazon-updates knowledge_sync --every-hours 6
```

---

## 嵌入 IvyeaOps

Ivyea Agent 既能独立当 CLI，也能作为 [IvyeaOps](https://github.com/Hector-xue/IvyeaOps) 工作台的本地智能底座启动：

```bash
ivyea serve --host 127.0.0.1 --port 8765     # 默认仅监听 localhost
```

本地 API 提供健康检查、能力矩阵、只读对话（含 SSE 流式）、持久会话、知识 / Skill / 检索、长任务 resume、运行时间线、workspace 与代码 Agent 只读能力，以及自发现契约（manifest / OpenAPI / stdio MCP 配置）。绑定 `0.0.0.0` 必须显式 `--allow-remote` 且提供 `--api-token`，避免误暴露。完整端点见 [docs/使用与操作文档.md](docs/使用与操作文档.md)。

---

## 安全与护栏

- **数据私有**：密钥、记忆、配置全在 `~/.ivyea/`（`.env` / `mcp.json` / `auth.json` 权限 600），不出本机、工具展示与 trace 自动脱敏 API key / token / secret。
- **写操作审核制**：广告写入、文件写、命令执行默认 dry-run，逐条人工审批；确定性硬护栏不交给模型。
- **本地安全策略**：`~/.ivyea/policy.json` 可配文件读写目录白名单、命令 allow/deny；内置拒绝明显高风险命令（`rm -rf /`、`git reset --hard` 等），`ivyea policy explain-command` 给风险分级。
- **沙箱**：`run_command` / `run_python` 在 *nix 上加资源限额（内存 / CPU / 文件 / 禁 core）+ cwd 限制 + 超时 + 输出截断。

---

## 配置与数据目录

```
~/.ivyea/
├── .env               主脑模型 key / 站点 / 目标 ACoS（权限 600）
├── settings.json      运行时设置（provider、思考深度、护栏阈值等）
├── auth.json          OAuth / Bearer 凭证（权限 600）
├── mcp.json           MCP 服务器与 dataSource 映射（权限 600）
├── policy.json        本地安全策略
├── memory.db / MEMORY.md   长期记忆
├── knowledge/ · skills/    用户知识卡与 Skill
└── tasks/ · workspaces/ · outputs/   长任务 / 项目索引 / 落盘输出
```

代码与数据分离：升级不会动 `~/.ivyea/` 里的任何配置与数据。

```bash
ivyea self status / backup / upgrade / uninstall   # 安装识别 / 备份 / 升级 / 卸载（默认 dry-run）
```

---

## 文档

- [docs/部署指南.md](docs/部署指南.md) —— 安装、升级、离线与内网部署
- [docs/使用与操作文档.md](docs/使用与操作文档.md) —— 各命令与本地 API 详解
- [docs/IvyeaAgent产品化路线图.md](docs/IvyeaAgent产品化路线图.md) —— 产品规划

如有问题，欢迎到 [Issues](https://github.com/Hector-xue/ivyea-agent/issues) 反馈，或扫码进群交流。
