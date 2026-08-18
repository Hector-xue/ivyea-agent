# ADR-0011 · 审批三档：只读 / 逐项审批 / 完全放行

- **日期**：2026-08-18（v1.15.0 之后）
- **状态**：已采纳（已落地）
- **依据**：2026-08-18 会话；IvyeaOps 任务台的档位选择器；对 `service.chat_stream`
  与 `permission.PermissionState` 的实读

## 背景

serve 侧此前只有两档：不传 `approval` 就是只读（`plan_mode=true`、`execute=false`），
传 `approval="remote"` 才放开写、且每一次写入都弹网页确认卡等人点。

问题出在**长任务**上。一轮里改十几个文件、批量否几十个词时，逐项审批意味着人得守在
屏幕前点十几次；点不完那一步就卡在服务端等到超时被拒。CLI 早就有这一档
（`--permission-mode approve-all` / `/auto-edit on` → `PermissionState.accept_edits`），
**只有网页这条路没有**——同一个 agent，从终端能一次性授权，从工作台不能。

还有一个更隐蔽的 bug：`_chat_messages` 里那句「[IvyeaOps 嵌入模式] 当前默认只读。……
不要在本轮直接执行」是**无条件**拼进系统提示词的。用户在界面上选了「逐项审批」，
模型收到的却仍是"你现在只读"，于是它只给方案不动手——开关看着变了，行为没变。

## 决策

`approval` 收敛成三档（`service._approval_mode` 负责归一，认不出的值一律落只读）：

| approval | plan_mode | execute | accept_edits | 语义 |
|---|---|---|---|---|
| `none`（默认） | true | false | false | 只读，写工具一律不落地 |
| `remote` | false | true | false | 逐项审批，每次写入弹确认卡 |
| `auto` | false | true | **true** | 完全放行，本轮不再逐条问人 |

配套：那句只读提示词跟着档位走，三档三句话。非流式入口 `chat_run` 只认 `auto`——
它没有回传确认卡的通道，`remote` 在那里仍然是只读（问不到人就不能写）。

## 理由

**为什么复用 `accept_edits` 而不是新开一条路**：写操作的闸门只有 `PermissionState`
一处。新开一条"跳过审批"的旁路，等于让写路径有两个入口，以后任何一次审批逻辑的收紧
都可能漏掉其中一个。`accept_edits` 是 CLI 用了很久的那把钥匙，网页只是拿到了同一把。

**为什么 `auto` 仍然要求 `plan_mode=false`**：两个开关打架时安全的那个赢。计划模式下
写工具在更外层就被拦住了，这里放行也落不了地——与其让它半开，不如明确保持只读。

**为什么不做成"agent 自己判断要不要问"**：那是把"这次改动要不要人看着"的判断交给
被授权方自己。授权的边界必须由人在发起时划定。

## 后果

- 前端多了一档明显危险的选项，所以它在界面上是**红色 + 闪电图标**的芯片，
  简约皮肤里也强制保留边框和底色（见 IvyeaOps `quiet-skin.css`）。
- `auto` 会让一轮里的所有写入静默发生。审计仍然照走（`audit.py` / 文件变更事件），
  这是事后可查的唯一凭据。
- 消费方契约变了：IvyeaOps 的 `ChatBody.approval` 与预设表的档位校验同步放开到三档，
  两处都补了用例——档位判错的方向只能是"少做"。
