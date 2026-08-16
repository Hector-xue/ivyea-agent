# IvyeaAgent 协作规约

面向在这个仓库里干活的**任何** AI 编码助手（Claude Code / Codex / Hermes / Cursor / Kiro …），
以及未来的自己。

> 各家工具读的文件名不同，这份是唯一的正本。`CLAUDE.md` 只是指向这里的入口。
> 改规约改这份，不要改入口文件。

## 这是什么

一个专做 Amazon 场景的 agent，既能在终端里对话使用（`ivyea chat`），也能被
[IvyeaOps](https://github.com/Hector-xue/IvyeaOps) 当成服务编程驱动。Python，随 IvyeaOps
一起打包分发。

立项理由见 [ADR-0001](./docs/decisions/0001-why-build-this.md)。

## 常用命令

```bash
python3 -m pytest                    # 全量测试
python3 -m pytest -m "not slow"      # 跳过真跑构建/模型的重测试
python3 -m ruff check .              # lint（CI 会卡）
ivyea chat                           # 交互式对话
ivyea chat -p "..." --output-format stream-json   # 非交互，结构化输出（IvyeaOps 走这条）
systemctl restart ivyea-agent        # 生产 serve（127.0.0.1:8765）改完重启
```

生产的 `agent-serve` 由 systemd 托管，**不要手工 nohup / setsid 起**。

## 工作方式：五步开发流

收到开发任务后按顺序执行，回复里显式标出步骤：

1. **理解需求边界** → 2. **制定技术方案** → 3. **复核方案并优化** → 4. **执行**（零省略）
→ 5. **校验**

**未经确认不得跳步直接写代码。**

配套要求：

- **动手前必须核实**消费方契约、真实数据来源、运行环境。第一步建立在猜测上，后面四步全错。
- **声明完成前必须自检消费方**。这条也是 agent 自身的行为准则
  （[ADR-0004](./docs/decisions/0004-self-verification-gate.md)）—— 对自己和对它要求一致。
- 终端交互的改动要用 **pty + pyte 真实跑**来验证，不要只读代码判断显示效果。

## 落档纪律（重要，每次会话都要做）

**做完事必须把结果写进文档。对话会消失，文档不会。** 不管你是哪个工具，这条都适用。

### 一、进仓库（会随开源发布出去，面向使用者和贡献者）

| 做了什么 | 落到哪 |
|---|---|
| 对使用者有影响的改动 | [CHANGELOG.md](./CHANGELOG.md) 的 Unreleased 段 |
| 方向性取舍、引入/移除依赖、踩到值得记住的坑 | 新建 [docs/decisions/](./docs/decisions/) 下的 ADR，编号顺延，并更新该目录 README 的索引表 |

### 二、维护者的私人记录（不在本仓库）

项目维护者另有一套本机私有的开发历史归档 —— 逐日时间线、里程碑、需求池，以及由 git 钩子
自动追加的提交流水。它由本机多个 AI CLI 的会话存档还原而来，**不进这个公开仓库**：里面含
密钥、业务数据和个人记录。

如果你在给维护者本人干活，那套归档的落档要求写在它自己的 README 里，按那份做。
如果你是外部贡献者，这一节与你无关 —— 按上面第一节做就够了。

## 发布纪律

- **未经明确批准不要 push / 开 PR / 合并 / 打 tag 发版。** 改完本地 commit 后停下汇报。
- 发版走 **GitHub Release 资产，不发 PyPI**。`self update` 也按这个前提设计：源码装的走
  `git pull`，其余回落 pip / pipx。
- 版本号只有一个来源（`ivyea_agent.__version__`），打 tag 时会校验版本与 tag 一致。
  历史上 `__version__` 漂移导致过「永远提示有更新」。

## 几条容易再犯的坑

- **`service stop` 曾经只认 pidfile 就报成功**，害得「升级了却还在跑旧代码」。判断服务停没停
  要看真实进程。
- **`pgrep` 会匹配到自己**，写检测脚本时注意排除。
- **知识卡的 `source_url` 必须真实核实过**，不能是模型编的链接。
- 知识库的 `index.json` 目前靠手工维护，改了知识卡要记得同步。
- **改完 serve 必须重启**才生效。
- 相似度**绝对阈值不可靠**；bge 系列推荐的查询前缀实测更差，不要照搬模型卡建议。
- 本机内存装不下 CUDA 版 torch，方案必须在 CPU 上可行。
- 跨平台断言不要硬编码路径分隔符（`tests/conftest.py` 里记了约定）。

## 想了解这个项目怎么走到今天

- [CHANGELOG.md](./CHANGELOG.md) —— 81 个版本的人话说明
- [docs/decisions/](./docs/decisions/) —— 8 份 ADR，为什么这么选
