# Ivyea Agent

自托管的**亚马逊运营 CLI Agent**。先做独立命令行工具（像 Hermes），成熟后嵌入 IvyeaOps 控制台。

> 哲学：**确定性规则引擎 + LLM 复核**；证据驱动+标签化；写操作审核制；护栏内置；数据私有。

## P1（当前）：只读广告巡检

输入一份亚马逊搜索词报告（CSV/xlsx），输出可执行的**只读巡检报告**：否词候选 / 放量 / 降 bid / Listing 反馈 / 观察 / 人工复核，每条带证据与置信度。**不会自动改广告。**

规则引擎复用了成熟的搜索词决策逻辑（`zach-search-term-report-analyzer`，已 vendor），LLM 只做复核（词分类/归因/护栏检查/措辞），不推翻数据。

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
脚本会自动装好 pipx 并把 `ivyea` 装到 PATH。国内慢可加镜像：`PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 前缀。装完重开终端，`ivyea config` 即可。

> 仓库私有期间 `git+https` 安装需你的 GitHub 凭据；转公开或发 PyPI 后任何人可直接装。

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

配置模型（像 Hermes/Claude 列出主流模型选）：
```bash
ivyea model            # 交互：列出国内外主流模型 + 登录制，选编号配置(含 key)
ivyea model list       # 只列清单
ivyea model deepseek-chat   # 按 id 直接切
```
已接入（OpenAI 兼容，可直接用）：OpenAI(GPT-4o)、DeepSeek、通义千问、Kimi/Moonshot、智谱GLM、豆包、MiniMax、OpenRouter、自定义端点。
规划中：Anthropic Claude / Google Gemini（原生 API）、Codex（ChatGPT 会员登录）/ Claude（订阅登录）等登录制。

### MCP 服务器（通用数据源；⚠️ 不是领星广告源）

> 更正（2026-06）：**领星广告数据不要走 MCP**——领星 MCP 无广告工具。领星请用上文
> `--from-lingxing`。本节的 MCP 客户端用于接入**其它**通用数据源（Sorftime/SIF/自建等）。
> 下文示例里凡出现 `领星` 仅为历史占位，实际请换成你的非领星 MCP 服务器名。

```bash
ivyea mcp add               # 对话式添加：名称 / 传输(http·sse·stdio) / URL / 鉴权(header·query)
ivyea mcp list
ivyea mcp tools <名称>       # 连上并列出该服务器暴露的工具(发现工具名/入参)
ivyea mcp call <名称> <工具> --args '{"asin":"B0.."}'   # 看某工具返回结构
ivyea mcp edit              # 编辑 mcp.json(填 dataSource 映射)
ivyea mcp remove <名称>
```

配置写入 `~/.ivyea/mcp.json`（权限 600，密钥只在你本机）。客户端是**通用的**——不绑死任何厂商工具名，由你配的 `dataSource` 映射驱动（approach c）。

**用 MCP 自动拉数（替代手动导 CSV）的步骤：**
1. `ivyea mcp add` 加好服务器（如领星）。
2. `ivyea mcp tools 领星` 看有哪些工具；`ivyea mcp call 领星 <工具> --args '{...}'` 看返回长什么样。
3. `ivyea mcp edit`，在该服务器下补 `dataSource` 映射：
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
4. 跑巡检：
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
- 斜杠命令：`/help /model /mcp /tools /clear /memory /exit`。
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

Hermes 同款：**SQLite FTS5 + 策展 markdown + 自策展**，本地自有（不依赖向量库/GBrain），存 `~/.ivyea/memory.db`、`~/.ivyea/MEMORY.md`、`~/.ivyea/account/<ASIN>.md`。

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
- LLM Provider 层：**OpenAI 兼容（DeepSeek 等）+ Claude 原生（官方 SDK，含 prompt caching）均可用**；`ivyea model` 选 `claude-opus`/`claude-sonnet`/`claude-haiku`（需 ANTHROPIC_API_KEY，可配自有网关 base_url）。Gemini/登录制规划中。⚠️ apimart 只生图、不能做 agent 主脑。
- 数据源：① 本地 CSV（vendored 规则引擎，离线/试用兜底）② **领星 OpenAPI 店铺维度（真实广告链路）** ③ 通用 MCP（任意数据源，**非领星广告**）。
- 记忆：SQLite FTS5 + 策展 markdown（中文回退 LIKE 子串检索）；**会话转录回忆 + 压缩摘要入库 + 自策展 nudge + 持久指令注入（USER.md/AGENTS.md，CLAUDE.md 同款，`/init` 生成）** 均已落地，已对真实 DeepSeek 验证指令被遵守。

## 状态（诚实盘点 2026-06）

**可用**：CSV 只读巡检；领星 OpenAPI 店铺维度巡检（真链路已实测：令牌+11店+报表 code=0）；**领星写入执行（否词/调bid/预算 + 审批 + operate开关 + 幅度闸 + 审计回滚）**——dry-run 与请求体已端到端验证，逻辑由 41 项 pytest 覆盖；通用 MCP 客户端；权限审批引擎；SQLite 记忆。

**对话式内核**：**流式输出 + 多步工具循环 + 成本/token 核算 + 计划模式 + 上下文压缩 + 会话 resume + 终端 Markdown 渲染**均已实现并对真实 DeepSeek 验证（含 prompt caching 实测命中）。聊天命令：`/plan`/`/approve`（计划模式）、`/cost`（用量）、`/compact`（压缩上下文）、`/raw`（切原始流式）。续接会话：`ivyea --continue` 或 `ivyea chat --resume [<id>]`。长对话自动压缩省 token。

**通用工具能力**：除广告巡检外，agent 还能 `read_file`/`list_dir`/`web_fetch`/`web_search`（只读自动放行）、`write_file`/`edit_file`/`run_python`（可用 pandas/openpyxl 读 Excel、算数）/`run_command`（写/执行经人工审批 + 计划模式拦截 + 沙箱限工作目录/超时/输出截断）。已对真实 DeepSeek 验证「读文件→run_python 计算→给结论」多步链路。

**视觉（对标 Claude Code）**：长任务用 `todo_write` 渲染 **Todo/Plan 面板**（☑/◐/☐ 实时勾选，真实 DeepSeek 已驱动跑通）；文件改动审批显示**彩色 diff**（红删绿增）；状态栏显示 模型/计划模式/上下文 token/累计 ¥；终端 Markdown 渲染；`NO_COLOR` 环境变量去色。

**体验/采用**：首次运行 **引导向导**（`ivyea onboard`：选模型→配 key→可选领星→AGENTS.md）；领星巡检带 **SQLite 缓存**（报表 7 天/实时 30 分，重复巡检秒回、尊重 1/s 限流，`ivyea lingxing cache clear` 清）+ **逐日进度条**；对话 **Ctrl+C 中断本轮不杀会话**；**每日成本护栏**（`config set daily_cost_limit_cny <元>`，超限暂停问人，`/cost` 看当日）；`run_command` 跨平台（Windows cmd / *nix bash）。

**健壮性**：provider 对 **429/5xx/网络错误自动重试+指数退避**（4xx 不重试，直接抛）；**降级链**——主脑重试后仍挂/无额度时，自动切到 `config set fallback_models <id,…>` 配的备用模型继续（流式仅在尚未吐字前可安全降级）。Anthropic 走官方 SDK 自带重试。

**工程化**：99 项 pytest（含**广告决策 eval 回归集**：样例报告的否词/放量/降bid 计数冻结，退化即变红）+ ruff lint 干净 + **GitHub Actions CI**（ubuntu/macos/windows × py3.9/3.11/3.12 矩阵，护 Windows 中文编码）+ sdist/wheel 构建通过 + tag 触发的 PyPI 发布工作流（待维护者打 tag）。

**尚未实现 / 待办**（不再标“✅完成”）：
- 领星**真实写入**仅 dry-run/单测验证，**尚未在生产真按下写**（需活跃广告店 + 你授权开 operate 实测）。
- Claude 原生已接（格式翻译+流式+caching，请求被 Anthropic 接受），**成功响应未实测**（需 ANTHROPIC_API_KEY）。
- Gemini/登录制（Codex/Claude 订阅）。
- 发 PyPI（已就绪，待维护者打 tag）。
- 嵌入 IvyeaOps（内核成库 + 反向 MCP）。

路线（总纲 M0–M7）：M0 止血+核实 ✅ → M1 领星适配层（只读）✅ → M2 领星写入执行+审批 ✅ → M1+ 内核(流式/成本/Plan) ✅ → M1++ 上下文压缩/resume/Markdown ✅ → 模型层(Claude 原生+caching) ✅ → 工具能力(文件/执行/web+沙箱+门控) ✅ → 记忆(回忆/摘要入库/自策展/持久指令) ✅ → 交互/视觉(Todo面板/彩色diff/状态栏) ✅ → 工程化(CI/eval/打包) ✅ → 嵌入 IvyeaOps。
