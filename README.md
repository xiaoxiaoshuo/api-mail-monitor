

## Manus 多账号监控模式

`metaso_monitor.py` 支持同时检查多个 Metaso API key。账号配置放在本机受保护文件 `~/.config/metaso-monitor/accounts.json`，格式为：

```json
[
  {"label": "账号A", "key": "mk-..."},
  {"label": "账号B", "key": "mk-..."}
]
```

默认请求 `https://metaso.cn/api/v1/usage`，默认在余额小于等于 `0` 时报警。每次检查会写入权限为 600 的 `~/.config/metaso-monitor/latest-report.json`，报告只保留账号标签、状态和余额，不保存 key。Manus ScheduleTask 负责每 5 分钟运行一次检查；发现任一账号余额不足或接口异常时，由 Manus 邮件服务发送汇总邮件，并列出所有异常账号，便于确认应充值的账号。

新增账号时只需把 key 加入受保护的账号文件，不要提交到 Git。当前代码保留原有 `monitor.py` 作为旧版 Resend/GitHub Actions 实现；新的定时任务使用 `metaso_monitor.py`，避免两套告警重复发送。

## 云吉 Task 阈值监控（新）

`balance_check.py` + `worker.py` 把余额检查做成 Cloudflare Worker 端点（每个账号一个 `/check/<账号>`），由 `yunji-task` 每 20 分钟调度：低于阈值返回 503 触发告警，恢复时返回 200 并带一次性 `recovered` 标记。部署、平台配置与费用见 [MIGRATION.md](MIGRATION.md)。

```sh
python3 -m unittest discover -s tests   # 离线测试
python3 deploy_cf.py                     # 上传 Worker（需要 CF_API_TOKEN 等）
```
