# ADR-0008 · 对标 DeepSeek Harness：借上下文经济学，不借插件架构

- **日期**：2026-08-16（v1.13.1 之后）
- **状态**：已采纳（方向已定，分批落地）
- **依据**：2026-08-16 会话；逐包比对本机安装的 `@deepseek-ai/dsh` 0.1.0-rc.6 源码

## 背景

DeepSeek 开源了 [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`），
一个 Node 写的 agent 框架，拆成 195 个单一职责的包，每个包都带中文 README，把设计理由写得很清楚。
它和本项目解决的是同一类问题，值得逐条比对。

比对下来，本项目有三个反复咬人的老问题，在 dsh 里都有成型的对策：

- **上下文说爆就爆**。全仓库找不到系统性的工具结果体积上限，只有零星的 `[:1000]` / `[:2000]`
  硬切片。一次 `read_file` 读到大文件、或一条 bash 吐出几万行，整坨直接进上下文。
- **压缩只有「调模型总结」一条路**（`context.py` 的 `compact()`）。花钱、慢、会把细节总结没，
  而且不可逆。
- **子 agent 的结论不可判定**。`agent_tools.py` 的 `t_dispatch_subagent` 返回自由文本，
  调用方没法可靠区分「查清楚了」「没查到」「卡住了」。

## 决策

**明确否掉插件架构本身**，只采纳具体机制，按下表分批落地：

| 优先级 | 采纳什么 | dsh 对应 | 改动落点 |
|---|---|---|---|
| P0 | 工具结果溢出落盘（spill） | `spill` + `spill-policy` + `output-retention` | `agent_loop.py` 的 `_record_tool_result` |
| P0 | 子 agent 结构化报告 | `tool-subagent-report` | `agent_tools.py` 的 `t_dispatch_subagent` |
| P1 | 确定性剪枝前置于模型压缩 | `compaction-tool-result-pruner` | `context.py` |
| P1 | 编辑前必读 + 带防护的写 | `fs-observation-policy` | 文件工具层 |
| P2 | 重复调用软提醒 | `repeat-tool-reminder` | `agent_loop.py` |
| P2 | 副作用前检查点 | `session-checkpoint-policy` | `sessions.py` + 主循环 |
| P3 | 事件溯源会话 | `session` + `session-projection` | 地基，需单独立项 |

三条关键细节，抄的时候不能丢：

1. **spill 给模型的替换不是哑巴省略号**，而是「首尾预览 + 落盘位置 + 取回指引」——
   模型要知道完整内容在哪、怎么捞回来。
2. **剪枝必须保留原始事件**。dsh 的剪枝是往仅追加日志里追加一条替换记录，原件不动，所以可回放、可逆。
3. **子 agent 报告无效就让流程失败，不要截断后当成功**。字段：
   `status: continue | complete | blocked` + 摘要 + 证据 + 后续步骤。

## 理由

**为什么否掉插件架构**：dsh 靠 Cordis 的 service/provider seam 把 195 个包拼起来，收益是每个关注点
可独立替换。本项目是 96k 行 Python 单体，硬套这套要重写地基，收益却主要落在「未来好替换」这种
远期价值上，眼下的痛点一个都不解决。**机制可以抄，形态不必抄。**

**为什么是这几项**：它们都打在已知痛点上，且改动集中。P0 两项尤其划算——`_record_tool_result` 是
所有工具结果的唯一咽喉，加一层拦截不用碰任何一个工具实现。

**已经有的不重复造**，比对时逐条确认过：

| 本项目已有 | 位置 | 结论 |
|---|---|---|
| 凭据只存引用不存值 | `config.py` 的 `key_env` | 与 dsh 的 `apiKeyEnv` 同一套设计 |
| token 估算 | `context.py` 的 `_est_text` | **比 dsh 强**，按 CJK 加权；dsh 是死板的 4 字符/token |
| 确定性工具护栏 | `agent_loop.py` 的 `_guard_tool_call` | 范围锁定、搜索死胡同恢复、导航未读计数，dsh 无对应物 |
| 子 agent 独立上下文 | `agent_tools.py` | 已是干净的 fresh spawn，不继承父对话 |

## 后果

**采纳 P0/P1 后，压缩链会变成分层的**：先确定性剪枝（不花钱、可逆），实在不够再上模型压缩。
现在是直接上模型。

**一个必须警惕的设计分歧**：dsh 的 `repeat-tool-reminder` 明确规定「不否决、不改写调用，只注入
逐级增强的提示，决定权仍在模型」。本项目的 `_guard_tool_call` 是硬拦截（直接返回
`ToolResult(False, "已拦截：…")`）。硬拦截治的是模型无视 prompt 的顽疾，有它的道理，**但模型有
正当理由重复调用时（比如轮询状态）会被锁死**。新增的重复调用检测走软提醒，不要再加硬拦截。

**P3 是地基改造，不要顺手做**。dsh 把会话做成仅追加事件日志、LLM 消息历史由它派生，压缩变成
非破坏性投影，[ADR-0005](./0005-stream-reliability.md) 里那套「断链改轮询落盘」的补丁在这个模型下
本来就不需要。但这是 96k 行代码的地基，正确做法是先在旁边加一条 append-only 日志、新功能走新路，
而不是重写 `sessions.py`。

参照物留在本机：`harness.ivyea.com` 跑着一份实物，源码在
`/root/.hermes/node/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/`，每个包都有中文 README。

本 ADR 覆盖的执行架构部分，与 [对标改造计划](../对标Codex-Claude-Hermes改造计划.md) 的 P7 是同一批工作。
