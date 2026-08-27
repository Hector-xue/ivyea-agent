# ADR-0023：每个入口先把 stdout 钉成 UTF-8

- 日期：2026-08-27
- 状态：已采纳

## 背景

v1.15.16 在 Windows 上出现一个"更新完就用不了"的故障：IvyeaOps 的系统配置页测试连接
报 `All connection attempts failed`，订阅登录页吐出这段 traceback：

```
File "ivyea_agent/service.py", line 2130, in run
  print(f"  {mark} {name}: {info.get('reason', '')}")
UnicodeEncodeError: 'gbk' codec can't encode character '✓'
```

崩点是 `serve` 打印常驻工人状态的那个 `✓`，而它是 v1.15.x 把巡检节拍器和飞书长连接
搬进 serve 时新加的 —— 所以**恰好是升级之后开始崩**。

真正的原因不在那个字符，在流的编码：Python 只有在 stdout 接的是**真实控制台**时才用
UTF-8；一旦被重定向（IvyeaOps 起 serve 时 `stdout=日志文件` 或 `stdout=DEVNULL`），
就退回系统 ANSI 代码页 —— 中文 Windows 是 GBK，英文 Windows 是 cp437。GBK 编不出 ✓，
cp437 连中文都编不出，而本项目的日志通篇中文。守护进程崩在第一行输出上，
端口没人监听，用户那边只剩一句"连不上"。

（`print` 写进 NUL 也会先编码，所以 `stdout=DEVNULL` 一样崩 —— 输出扔不扔得掉不重要。）

## 决策

新增 `ivyea_agent/stdio_utf8.force_utf8()`：把 `sys.stdout` / `sys.stderr`
reconfigure 成 `encoding="utf-8", errors="replace"`，幂等、失败静默。
**凡是会 print 的入口先调它** —— 目前是 `cli.main()` 和 `service.run()`。
spawn 子进程的地方再补一道 `PYTHONUTF8=1`。

## 理由

否掉的两个方案：

- **把 ✓ 换成 ASCII。** 治标。`reason` 文案本身是中文，换掉符号只是把崩溃从中文
  Windows 挪到英文 Windows，而且下一个写 print 的人不会知道有这条禁令。
- **只在打包入口修一次。** IvyeaOps 早就在 `ivyeaops_server.py` 里 reconfigure 过了，
  但那段代码排在 `agent-serve` 分支**之后**，agent 永远轮不到 —— 修在别人家的入口，
  就会有下一个入口漏掉。护栏要长在 agent 自己身上。

`errors="replace"` 是最后一道保险：宁可显示成 `?`，也绝不让一个字符掀翻守护进程。

## 后果

- 真实控制台上这是空操作（PEP 528 起 Windows 控制台本来就走 UTF-8/UTF-16），
  不会把中文变成乱码。
- 日志文件从此固定是 UTF-8。读端本来就按 UTF-8 读（`service_log_tail`、
  IvyeaOps 的 `decode("utf-8", "replace")`），方向一致。
- 回归测试 `tests/test_stdio_utf8.py` 故意把 stdout 换成 strict 的 GBK 流：
  摘掉护栏就复现原样的 `UnicodeEncodeError`。
