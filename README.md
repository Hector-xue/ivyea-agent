# Ivyea Agent

自托管的**亚马逊运营 CLI Agent**。先做独立命令行工具（像 Hermes），成熟后嵌入 IvyeaOps 控制台。

> 哲学：**确定性规则引擎 + LLM 复核**；证据驱动+标签化；写操作审核制；护栏内置；数据私有。

## P1（当前）：只读广告巡检

输入一份亚马逊搜索词报告（CSV/xlsx），输出可执行的**只读巡检报告**：否词候选 / 放量 / 降 bid / Listing 反馈 / 观察 / 人工复核，每条带证据与置信度。**不会自动改广告。**

规则引擎复用了成熟的搜索词决策逻辑（`zach-search-term-report-analyzer`，已 vendor），LLM 只做复核（词分类/归因/护栏检查/措辞），不推翻数据。

## 安装

```bash
pipx install ivyea-agent        # 或开发态： pip install -e .
```

需要 Python ≥ 3.9。依赖：pandas / openpyxl / httpx。

## 配置

主脑模型 P1 用 DeepSeek（OpenAI 兼容、便宜、够用）。在 `~/.ivyea/.env` 写入：

```
DEEPSEEK_API_KEY=sk-xxxx
```

查看/修改配置：

```bash
ivyea config show
ivyea config set target_acos 0.3
ivyea config set site US
```

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
