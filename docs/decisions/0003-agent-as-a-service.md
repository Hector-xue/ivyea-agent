# ADR-0003 · 从 CLI 变成可编程驱动的服务

- **日期**：2026-06-23（服务化）/ 2026-07-03（stream-json 补齐）
- **状态**：已采纳
- **依据**：提交 `Expose embedded read-only chat API`、`Productize embedded agent runtime`、
  `feat(stream-json): chat -p 增加 --output-format stream-json 结构化 NDJSON 输出`

## 背景

IvyeaOps 要驱动 agent 干活。最初的做法是每次起一个子进程、跑一次 `ivyea chat`、**解析它打给
人看的终端输出**。这套做法的问题很直接：

- 终端输出是给人看的，加了颜色、动画、进度条，解析起来极其脆弱
- 每次起进程都要重新加载模型配置、知识索引、记忆，慢
- 无法知道 agent 跑到哪一步了，只能等它结束
- 长任务没有中途反馈

## 决策

两步走：

1. **服务化**（v0.5.x）—— agent 常驻运行，对外暴露只读 chat API、只读 MCP server、
   任务状态 API。IvyeaOps 调 HTTP，不再起子进程。
2. **结构化输出**（v1.2.0）—— `chat -p --output-format stream-json` 输出 NDJSON 事件流：
   每一步工具调用、每一段正文、每一次审批请求都是一条独立事件。

## 理由

- 给机器看的接口和给人看的界面必须分开，混在一起两边都做不好
- 事件流让工作台能做出真正的过程可视化（后来的任务台分区、产物栏 diff 都依赖它）
- 常驻服务才能复用已加载的索引和记忆

## 后果

- IvyeaOps 的 runner 全面改走 stream-json
- 后续所有面向工作台的能力都以「发一个结构化事件」的形式提供：结构化步骤（v1.9.0）、
  文件变更事件（v1.10.x）、思考流（v1.10.3）
- 服务化带来了新的一类问题 —— 生命周期管理。`service stop` 只认 pidfile 导致「谎报成功」
  这个 bug 直到 2026-08-16（v1.13.1）才被发现并修掉，期间造成过「升级了但仍在跑旧代码」
- 生产环境最终交给 systemd 托管，不再手工 nohup 启动
