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
| [0017](./0017-store-patrol-and-approval-loop.md) | 店铺巡检分层；证据门槛由动作可逆性决定；审批状态归 agent | 2026-08-22 |
| [0018](./0018-multi-store-patrol.md) | 多店铺巡检：能力边界不算故障；变体合并后再推送 | 2026-08-23 |
| [0019](./0019-feishu-config-is-agent-owned-and-ui-driven.md) | 飞书配置归 agent 存、界面来写；relay 以它为准，env 只兜底 | 2026-08-23 |
| [0020](./0020-cadence-tiers-review-reports-and-rules-without-data.md) | 节奏改 1h/12h/日/周/月；周报只回顾不派活；没数据也照写规则 | 2026-08-23 |
| [0021](./0021-amazon-official-api-built-before-the-account.md) | 亚马逊官方 API 先建好，不等某台机器有账号；契约逐条来自官方文件 | 2026-08-23 |
| [0022](./0022-promotion-rules-without-a-write-channel.md) | 促销规则只报不改（没有写接口），且必须先报「数据还能不能信」 | 2026-08-23 |
| [0023](./0023-stdio-is-utf8-on-every-entry-point.md) | 每个入口先把 stdout 钉成 UTF-8（Windows 重定向后默认 GBK，一个 ✓ 崩掉 serve） | 2026-08-27 |
| [0024](./0024-a-running-turn-is-a-server-side-fact.md) | 正在跑的那一轮是服务端的事实：活轮事件日志 + 任何退出路径都落盘 | 2026-08-27 |
| [0025](./0025-memory-is-runtime-driven.md) | 记忆由运行时驱动，不等模型想起来去查 | 2026-08-27 |
| [0026](./0026-the-plan-is-runtime-state.md) | 计划是运行时状态，不是模型的记忆 | 2026-08-28 |
| [0027](./0027-thinking-critique-and-evidence.md) | 思考按轮定档、自查由运行时兜底、证据要落盘 | 2026-08-28 |
| [0028](./0028-agent-authored-skills.md) | 技能改成 agent 自己能读全、能写、能沉淀 | 2026-08-28 |
| [0029](./0029-roles-budget-and-curation.md) | 子 agent 分工、预算按「干活」算、技能库要有人管 | 2026-08-28 |
| [0030](./0030-compaction-must-be-able-to-help.md) | 越过压缩阈值 ≠ 压得动 | 2026-08-28 |
| [0031](./0031-a-running-turn-can-still-be-talked-to.md) | 一轮跑着的时候，人还能说话（也能叫停） | 2026-08-28 |
| [0032](./0032-session-attachments-are-not-knowledge.md) | 会话附件不是知识：只抽正文、不进知识库 | 2026-08-30 |
| [0033](./0033-retrieval-fusion-and-answer-level-evals.md) | 中文查询要真的能检索，评测要测回答而不是召回 | 2026-08-30 |
| [0034](./0034-negation-guardrails-live-in-ivyea-not-the-vendored-script.md) | 否词护栏放在自己这边，不动 vendor 进来的脚本 | 2026-08-30 |
| [0035](./0035-knowledge-gaps-must-build-their-own-priority-list.md) | 补哪些知识卡，让数据自己排，别替人判 | 2026-08-30 |
| [0036](./0036-illustrations-come-from-og-image.md) | 回答里的配图去网页上找，不去模型里画 | 2026-09-02 |
| [0037](./0037-quick-lane-for-knowledge-questions.md) | 知识型提问单开一条车道，只挂只读检索工具 | 2026-09-02 |
| [0038](./0038-injection-precision-over-recall.md) | 注入精度优先于召回率，且必须先能度量 | 2026-09-02 |
| [0039](./0039-goal-mode-holds-the-verdict.md) | 目标模式：判定权归运行时，不归模型 | 2026-09-03 |
| [0040](./0040-intent-not-materials.md) | 判意图只看正文，URL/路径/代码是素材不是意图 | 2026-09-10 |
| [0041](./0041-state-the-environment-dont-make-it-guess.md) | 运行环境是确定事实，直接告诉模型别让它试 | 2026-09-10 |

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
