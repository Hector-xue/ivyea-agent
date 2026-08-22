# 架构决策记录（ADR）

每份文件记一个决策：当时的背景、决定了什么、为什么这么选、后来付出了什么代价。

git log 记得住「改了什么」，记不住「为什么不选另一条路」。这个目录补的就是后者。

## 什么时候写一份新的

- 选了 A 方案而否掉了 B 方案，且这个选择会长期影响后面的代码
- 引入或移除一个重量级依赖
- 改变了某个东西的边界（谁负责什么、数据从哪来）
- 踩了一个坑，而这个坑的根因值得让未来的自己记住

日常改 bug、加功能不用写 —— 那些看 [CHANGELOG](../../CHANGELOG.md) 和
维护者本机私有的开发时间线。

## 索引

| 编号 | 决策 | 日期 |
|---|---|---|
| [0001](./0001-why-build-this.md) | 为什么要自己做一个 agent | 2026-06-16 |
| [0002](./0002-shadow-mode.md) | 影子模式：动真钱之前先用数据换信任 | 2026-06-18 |
| [0003](./0003-agent-as-a-service.md) | 从 CLI 变成可编程驱动的服务 | 2026-06-23 |
| [0004](./0004-self-verification-gate.md) | 完成前自验证门禁 | 2026-07-05 |
| [0005](./0005-stream-reliability.md) | 长任务不能因为客户端断开就白跑 | 2026-07-16 |
| [0006](./0006-memory-and-retrieval.md) | 记忆三层架构与语义双路召回 | 2026-08-15 |
| [0007](./0007-vision-tier-chain.md) | 视觉三档降级链 | 2026-08-16 |
| [0008](./0008-borrow-from-deepseek-harness.md) | 对标 DeepSeek Harness：借上下文经济学，不借插件架构 | 2026-08-16 |
| [0009](./0009-skill-md-frontmatter-and-external-roots.md) | 技能改用通行的 SKILL.md + frontmatter，并支持外部技能库 | 2026-08-17 |
| [0010](./0010-request-routing-lanes.md) | 按这句话的性质选路线：闲聊 / 板块直达 / 常规 | 2026-08-18 |
| [0011](./0011-approval-tiers.md) | 审批三档：只读 / 逐项审批 / 完全放行 | 2026-08-18 |
| [0012](./0012-context-usage-snapshot.md) | 上下文用量由 serve 现算并上报，明说是估算 | 2026-08-18 |
| [0013](./0013-attachments-belong-to-the-user-message.md) | 调用方给的附图内容并进 user 消息，不放 system | 2026-08-21 |
| [0014](./0014-per-turn-model-override.md) | 主脑可以按轮次覆盖，覆盖失败绝不回落 | 2026-08-21 |
| [0015](./0015-web-login-for-subscription-providers.md) | 订阅制 provider 的登录开成 HTTP，凭据不出服务端 | 2026-08-21 |
| [0016](./0016-subprocess-env-allowlist.md) | 子进程环境走白名单；许可证定为 MIT | 2026-08-22 |

相关的工作台侧决策见
[IvyeaOps 的 ADR 目录](https://github.com/Hector-xue/IvyeaOps/tree/main/docs/decisions)，
其中 ADR-0009（自己做 agent）是本项目的立项依据。

## 模板

```markdown
# ADR-000X · 一句话说清决定了什么

- **日期**：
- **状态**：已采纳 / 已废弃 / 被 ADR-00YY 取代
- **依据**：提交、PR 或会话日期

## 背景
当时遇到了什么问题。写清楚约束，不写方案。

## 决策
决定做什么。一两句话。

## 理由
为什么是这个而不是别的。把否掉的选项也写出来。

## 后果
这个决定带来了什么，包括代价和后来踩的坑。
```
