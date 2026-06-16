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

配置模型也可单独用：
```bash
ivyea model                      # 交互配置 provider/模型/key
ivyea model deepseek:deepseek-chat   # 直接切
```

### MCP 服务器（对话式配置 + 自动拉数）

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

## 设计 / 路线图

- 架构与方法论：见 IvyeaOps 知识库 `ivyea-agent/架构方案`、`amazon-ops/*`。
- LLM Provider 层：多模型统一接口（P1 接 DeepSeek，预留 OpenAI/Anthropic/Gemini/Ollama）。⚠️ apimart 只生图、不能做 agent 主脑。
- MCP：P1.5 接入「领星 ERP MCP」直接读广告报表（替代手动导 CSV）；写操作走网关 + 审核制（P2）。
- 设计 v2（架构学 Hermes、交互学 Claude Code）：见知识库 `ivyea-agent/设计v2`。
- 记忆（P3）：**SQLite FTS5 + 策展 markdown + 摘要**（Hermes 同款，自有，不用向量库/GBrain）。
- 路线：P1 只读巡检 ✅ → P1.5 通用 MCP ✅ → P2 审核制执行 ✅ → P2.5 对话式+权限审批 ✅ → P3 记忆 → P4 嵌入 IvyeaOps → P5 多 ASIN/多店 + 自学习。

## 状态

P1/P1.5/P2/P2.5 已完成（只读巡检、通用 MCP、审核制执行、对话模式+权限审批）。
待验证真链路：DeepSeek 主脑 key（对话/复核）、真实 MCP 读写。P3 记忆进行中。
