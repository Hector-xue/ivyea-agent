# ADR-0015 · 订阅制 provider 的登录开成 HTTP，凭据不出服务端

- **日期**：2026-08-21
- **状态**：已采纳
- **依据**：IvyeaOps 想让不会用命令行的人也能接自己的 Claude / ChatGPT / Gemini 订阅

## 背景

模型接入分两类：填 API key 的，和要走 OAuth 的订阅制（Claude 订阅、OpenAI Codex、
Gemini Code Assist、Qwen、GitHub Copilot）。前者在 IvyeaOps 的系统配置里填个输入框
就完事；后者此前只有一条路 —— `ivyea model auth <id> --login`。

于是"不会用 CLI 的人根本接不上自己已经付过钱的订阅"。

技术上的拦路虎是：登录函数是**阻塞**的。`qwen_device_code_login` /
`codex_device_code_login` 会在函数里 `while` 轮询到用户在浏览器里点完为止，最长
十几分钟；`anthropic_oauth_login` / `google_oauth_login` 会 `input()` 等人粘码。
HTTP 请求挂不住这些。

## 决策

1. 把四条登录流程各拆成一对**纯函数**：`*_start()` 返回"用户需要看到的东西 + 一个
   ctx"，`*_poll(ctx)` / `*_complete(ctx, raw)` 接着往下走。ctx 由调用方保管。
2. 原来的阻塞函数在这些之上重新拼起来，**CLI 行为逐字不变**（34 条既有测试原封不动
   跑绿，就是这条的证据）。
3. serve 侧新增 `GET /v1/auth` + `POST /v1/auth/{id}/start|poll|complete|logout`，
   ctx 存在进程内的会话池里（20 分钟 TTL、20 条上限）。
4. **凭据一律不出服务端**：PKCE verifier、state、device_code、access_token 都只留在
   服务端；返回给调用方的只有授权链接、user_code、验证地址。

## 理由

**为什么不是"在服务端起个线程跑原来那个阻塞函数"**：省事，但取消、超时、并发、
进度回报全要另起一套，而且失败原因传不回调用方。拆成 start/poll 之后每一步都是
一次普通的函数调用，可测、可重试、能明确报错。

**为什么 ctx 由调用方保管而不是塞进模块全局**：oauth_auth 是纯协议层，给 CLI 和
serve 共用。往里面塞全局状态，两个调用方就会互相踩。

**Gemini 那家的特殊情况**：它默认要在本机起一个 `127.0.0.1:8085` 的回调服务。远程用
的时候这条走不通 —— 浏览器在用户自己的机器上，`127.0.0.1` 指的是用户那台。但回调
失败不影响：浏览器地址栏里带着 `?code=…`，把整条 URL 粘回来即可。唯一的硬约束是
换 token 时的 `redirect_uri` 必须和授权时**逐字一致**，所以它跟着 ctx 一起走。

## 后果

- 会话池装的是凭据，所以带 TTL 和上限，成功之后立刻销毁。它是进程内存 —— serve
  重启后进行中的登录会失效，重来一次即可（这比把凭据落盘好）。
- Copilot 的 token 改写 `COPILOT_GITHUB_TOKEN`，不碰 `GH_TOKEN` / `GITHUB_TOKEN`。
  那两个是 gh CLI 和 CI 在用的，被顺手改掉时完全看不出是谁干的。退出登录同理，
  只清自己那一个。
- **单租户语义**：token 存在服务器的 `~/.ivyea/auth.json`，agent 全局共用。也就是
  谁登录，这台 agent 上所有对话和定时任务都在用谁的订阅额度。调用方（IvyeaOps）
  要把这句话摆在界面上，并把入口限制给管理员。
- 各家订阅条款通常是限个人使用的，接进多人共用的工作台是不是合规，由部署者判断。
  这里只提供能力，界面上要如实提醒。
