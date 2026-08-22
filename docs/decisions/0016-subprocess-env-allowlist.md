# ADR-0016 · 子进程环境走白名单；许可证定为 MIT

- **日期**：2026-08-22
- **状态**：已采纳

## 背景

两件本来无关的事，因为同一个契机被一起发现，也就一起定了。

### 一、子进程能读走本机全部密钥

`mcp_client._ensure_stdio()` 的 `Popen` 不带 `env=`，`hooks._hook_env()` 直接
`dict(os.environ)` —— 两者都把父进程的**全部**环境交给子进程。里面有
`DEEPSEEK_API_KEY`、领星凭据、各家 provider 的 key。

自己手配一两个 MCP server 的年代，这不算问题：那些命令是自己挑的。但方向已经变了 ——
`ivyea mcp add` 让接入任意第三方 server 变成一条命令，hook 也开始有人从别处抄脚本。
**一个"查快递"的 server 没有任何理由看得见 DeepSeek 的 key。**

对照组：DeepSeek Harness 的 `dsh-mcp-client`，其配置字段的文档写的是
"Extra env vars merged on top of **scrubbed** ambient env" —— 它清洗，我们不清洗。

同时暴露出一个一直没人发现的 bug：**`mcp.json` 里的 `env` 字段被静默忽略**。
`MCPClient.__init__` 压根没读它。之所以拖了这么久没暴露，是因为多数 server 到真正
调用工具时才校验密钥（本机 sellersprite 的 `_key()` 就在 `_request` 里才调），
`list_tools` 一路能过 —— **表现是"工具列得出来，一调就说没配密钥"**，
排查时很容易往 server 自己身上找。

### 二、仓库没有许可证

没有 LICENSE 文件，`pyproject.toml` 也没有 `license` 字段。这在法律上等于
**保留所有权利** —— 别人不能合法使用、fork，更不能基于它写插件或做二次开发。
对一个自称开源、且希望别人来扩展的项目，这是地基漏了。

## 决策

### 1. 子进程环境默认走白名单

只放行「缺了子进程跑不起来或行为不对」的通用项，判据是**必需**而非方便：

- 类 Unix：`PATH` `HOME` `USER` `SHELL` `TERM` `LANG` `LC_*` `TZ` `TMPDIR` `TEMP` `TMP`
- Windows：另加 `SYSTEMROOT` `WINDIR` `COMSPEC` `PATHEXT` `SYSTEMDRIVE` `APPDATA`
  `LOCALAPPDATA` `USERPROFILE` `PROGRAMFILES*` 等 —— 这些缺任何一个，子进程基本起不来
- 两边都放行 `PYTHONIOENCODING` / `PYTHONUTF8`：不给，子进程读写中文会按 ASCII 崩

其余一律要在配置里显式声明，三层逃生舱从窄到宽：

| 字段 | 含义 |
|---|---|
| `env` | 直接给值，或用 `${VAR}` 从父环境取一个 |
| `env_passthrough` | 列几个键名原样带过去（`env` 的简写） |
| `inherit_env` | 整份继承，等同改动前的行为 |

MCP server 写在 `mcp.json` 的服务器条目里，hook 写在 `hooks.json` 的条目里。
**环境三件套跟着条目走，不设全局开关** —— 给全局开一个口子等于没清洗。

### 2. 不放行 `PYTHONPATH`

它既泄漏本机目录结构，又是一条**代码注入路径**：子进程会从那里 import。
真需要就在 `env` 里显式写一次。

### 3. `${VAR}` 展开只认花括号形式，不认 `$VAR`

裸 `$` 在真实的值里太常见 —— 密码、正则、Windows 的 `%PATH%` 之外的各种字面量。
按变量展开会把它们**悄悄改掉**，而这种错误在运行时长得像"密钥不对"。
`${VAR}` 是明确的意图声明，误伤概率低得多。

父环境没有该变量时展开成**空串**，不保留 `${VAR}` 字面量：子进程拿到一个长得像占位符
的值，多半会当真值用下去，错得更隐蔽；空串至少让它在第一步就报"没配"。

### 4. 许可证：MIT

同仓库群里 IvyeaOps 是 AGPL-3.0（要保护的产品），ivyea-translate 是 MIT（要传播的工具）。
本仓属于后者：**它的价值在于被用、被扩展**，一个会劝退插件作者和企业用户的传染性协议
和这个目标冲突。

写法上用 `license = { text = "MIT" }` 而不是 PEP 639 的 SPDX 字符串 ——
后者要 setuptools ≥ 77，本仓 `build-system.requires` 是 `>=68`。

## 代价

**这是行为变更，会打破依赖环境变量的现有配置。** 某个 MCP server 或 hook 脚本原本靠
父进程里的变量工作，升级后会拿不到 —— 而它多半只会报一句自己的"未配置"，指不到真正
的原因上。

接受这个代价，理由是：默认不安全的东西，靠"用户自己去开"是开不起来的。
迁移成本是在自己的条目里加一行 `env` 或 `env_passthrough`，
CHANGELOG 和 README 都写了怎么改。

另一层代价是**白名单本身会长期需要维护**：将来某类 server 普遍依赖某个变量时，
要判断它是"必需"还是"方便"。判据留在 `subproc_env.py` 的模块 docstring 里。

## 验证

先写测试确认红、再实现转绿（`tests/test_subproc_env.py`，21 例），其中两例是端到端：
真起一个子进程让它 dump 自己的环境、真跑一个 hook 脚本，断言拿不到密钥、拿得到显式传的。

实测子进程环境从 45 个键收敛到 8 个；全量回归 1199 → 1220 passed，零回归。
