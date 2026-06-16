# Ivyea Agent

自托管的**亚马逊运营 CLI Agent**。先做独立命令行工具（像 Hermes），成熟后嵌入 IvyeaOps 控制台。

> 哲学：**确定性规则引擎 + LLM 复核**；证据驱动+标签化；写操作审核制；护栏内置；数据私有。

## P1（当前）：只读广告巡检

输入一份亚马逊搜索词报告（CSV/xlsx），输出可执行的**只读巡检报告**：否词候选 / 放量 / 降 bid / Listing 反馈 / 观察 / 人工复核，每条带证据与置信度。**不会自动改广告。**

规则引擎复用了成熟的搜索词决策逻辑（`zach-search-term-report-analyzer`，已 vendor），LLM 只做复核（词分类/归因/护栏检查/措辞），不推翻数据。

## 安装与部署

跨平台纯 Python（≥3.9），依赖 pandas / openpyxl / httpx，**无需 Node/数据库**。推荐用 `pipx` 装成独立隔离环境，命令全局可用、不污染系统 Python。

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

### MCP 服务器（对话式配置）

```bash
ivyea mcp add           # 对话式添加：名称 / 传输(http·sse·stdio) / URL / 鉴权(header·query)
ivyea mcp list
ivyea mcp remove <名称>
```

配置写入 `~/.ivyea/mcp.json`。P1.5 的 MCP 客户端会读取它直连领星等服务拉广告数据（替代手动导 CSV）。

## 使用

```bash
ivyea patrol 搜索词报告.csv --asin B0XXXXXXXX --site US --target-acos 0.3
ivyea patrol 报告.csv --no-llm        # 只跑规则引擎，跳过 AI 复核
```

未配置模型 key 时会自动降级为「仅规则引擎」结论，不报错。报告同时保存为 `.md`。

## 设计 / 路线图

- 架构与方法论：见 IvyeaOps 知识库 `ivyea-agent/架构方案`、`amazon-ops/*`。
- LLM Provider 层：多模型统一接口（P1 接 DeepSeek，预留 OpenAI/Anthropic/Gemini/Ollama）。⚠️ apimart 只生图、不能做 agent 主脑。
- MCP：P1.5 接入「领星 ERP MCP」直接读广告报表（替代手动导 CSV）；写操作走网关 + 审核制（P2）。
- 路线：P1 只读巡检 → P2 审核制执行（一键确认改 bid/否词）→ P3 记忆(GBrain)+实时触发 → P4 嵌入 IvyeaOps → P5 多 ASIN/多店规模化。

## 状态

P1 MVP（只读巡检）。写操作、自动调度、对话模式、嵌入控制台均为后续阶段。
