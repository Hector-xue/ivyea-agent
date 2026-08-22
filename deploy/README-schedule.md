# 定时巡检的安装（需人工确认后执行）

这两个单元文件**不会**被自动安装。安装会改动系统状态，请自行确认后执行。

## 1. 先把任务配好（这一步不改系统）

```bash
# L1 快照层：每 20 分钟（库存断货、活动被暂停、预算被外部改动）
ivyea schedule set l1-1863 store_l1 --every-minutes 20 --sid 1863

# L2 日内层：每小时（花费突增、曝光归零、点击无单）
ivyea schedule set l2-1863 store_l2 --every-hours 1 --sid 1863

# 早报：每天一次（配合 timer 的唤醒节奏，实际约在整点后 5 分钟内触发）
ivyea schedule set daily-1863 store_daily --every-hours 24 --sid 1863

# 过期审批清扫：每小时
ivyea schedule set appr-expire approvals_expire --every-hours 1

ivyea schedule list
```

先手动验证一轮，确认输出符合预期：

```bash
ivyea schedule run-due
```

## 2. 安装 timer

```bash
cp deploy/systemd/ivyea-schedule.service deploy/systemd/ivyea-schedule.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ivyea-schedule.timer
systemctl list-timers ivyea-schedule.timer
```

## 3. 排查

```bash
journalctl -u ivyea-schedule.service -n 100 --no-pager
systemctl start ivyea-schedule.service   # 立即跑一次
```

## 为什么频率写在 schedule.json 而不是 timer 里

timer 只负责「到点了没」，`run-due` 负责「该跑哪些」。改巡检频率只需
`ivyea schedule set`，不用 `daemon-reload`，也不会因为改一个任务的节奏
而影响其它任务。
